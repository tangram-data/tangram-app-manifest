import json
import os
import unittest
from unittest.mock import patch
from tangram_app import TangramApp, CapabilityGraph, hub_receipts
from test_sdk import graph_dict
from test_hub import run
import test_hub

class SimpleHubTest(unittest.TestCase):
    setUp = test_hub.HubTest.setUp

    def local(self):
        app = TangramApp.from_graph(CapabilityGraph.from_dict(graph_dict()))
        p = patch('tangram_app.cli_hub._installed', return_value=({'root': str(self.home)}, app))
        p.start()
        self.addCleanup(p.stop)

    def test_simple_read_without_flags(self):
        self.local()
        from tangram_app.cli_parser import build_parser
        from tangram_app.cli_hub import run as hub_run
        with patch('tangram_app.cli._run', return_value={'orders': []}) as execute:
            args = build_parser().parse_args(['app', 'call', 'com.example/orders#Order.List', '--input', '{}'])
            result = hub_run(args)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['target']['lane'], 'local')
        self.assertEqual(execute.call_args.args[0].package_digest, 'sha256:fixture')
        self.assertIsNotNone(hub_receipts.get(result['invocationId']))

    def test_timeout_resume_ignores_changed_ambient(self):
        self.local()
        def fail(args, arguments):
            self.assertEqual(hub_receipts.get(args.invocation_id)['action'], 'com.example/orders#Order.Approve@approveOrder')
            raise OSError('response lost')
        with patch('tangram_app.desktop_invocation.call', side_effect=fail):
            status, result = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":1}')
        self.assertNotEqual(status, 0)
        data = result['data']
        self.assertEqual(data['state'], 'unknown')
        with patch.dict(os.environ, {'TANGRAM_INSTANCE': 'unrelated', 'TANGRAM_WORKSPACE': 'production'}), patch('tangram_app.desktop_invocation.call', return_value={'state': 'succeeded'}) as execute:
            status, resumed = run('resume', data['invocationId'])
        self.assertEqual(status, 0, resumed)
        self.assertEqual(execute.call_args.args[0].invocation_id, data['invocationId'])
        self.assertEqual(execute.call_args.args[1], {'orderId': 1})
        self.assertEqual(resumed['data']['target']['lane'], 'local')

    def test_resume_refuses_contract_drift(self):
        self.local()
        with patch('tangram_app.desktop_invocation.call', return_value={'state': 'unknown'}):
            _, result = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":1}')
        graph = graph_dict(); graph['package']['digest'] = 'sha256:changed'
        changed = TangramApp.from_graph(CapabilityGraph.from_dict(graph))
        with patch('tangram_app.cli_hub._installed', return_value=({'root': str(self.home)}, changed)), patch('tangram_app.desktop_invocation.call') as execute:
            status, result = run('resume', result['data']['invocationId'])
        self.assertNotEqual(status, 0)
        self.assertEqual(result['error']['code'], 'contract_changed')
        execute.assert_not_called()

    def test_conflicting_context_and_input_are_refused(self):
        self.local()
        self.assertNotEqual(run('search', '', '--local', '--target-ref', self.pin['targetRef'])[0], 0)
        with patch('tangram_app.desktop_invocation.call', return_value={'state': 'succeeded'}) as execute:
            _, first = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":1}')
            status, second = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":2}', '--invocation-id', first['data']['invocationId'])
            self.assertNotEqual(status, 0)
            self.assertEqual(second['error']['code'], 'invocation_conflict')
            self.assertEqual(execute.call_count, 1)

    def remote(self):
        (self.home / '.credentials').write_text(json.dumps([{'instance': 'dev', 'url': 'localhost', 'token': 'one'}]))
        env = patch.dict(os.environ, {'TANGRAM_INSTANCE': 'dev', 'TANGRAM_WORKSPACE': 'demo'})
        env.start(); self.addCleanup(env.stop)

    def test_remote_refuses_ambient_mutation(self):
        self.remote()
        def server(cred, method, path, body, **kwargs):
            self.assertIn('/workspaces/demo/', path)
            if path.endswith('/search'):
                return {'candidates': [{'action': 'g/a#Thing.Do@do', 'contract': 'digest'}], 'nextOffset': None}
            if path.endswith('/inspect'):
                return {'effect': 'Reversible', 'requiresConfirmation': False}
            self.fail('mutation dispatched')
        with patch('tangram_app.cli_hub.os_request', side_effect=server):
            status, result = run('call', 'g/a#Thing.Do', '--input', '{}')
        self.assertNotEqual(status, 0)
        self.assertEqual(result['error']['code'], 'target_confirmation_required')

    def test_remote_read_freezes_context(self):
        self.remote()
        def server(cred, method, path, body, **kwargs):
            self.assertIn('/workspaces/demo/', path)
            if path.endswith('/search'):
                os.environ['TANGRAM_WORKSPACE'] = 'changed'
                return {'candidates': [{'action': 'g/a#Thing.List@list', 'contract': 'digest'}], 'nextOffset': None}
            if path.endswith('/inspect'):
                return {'effect': 'Stateless', 'requiresConfirmation': False}
            self.assertEqual(body['action'], 'g/a#Thing.List@list')
            self.assertEqual(body['contract'], 'digest')
            self.assertIsNotNone(hub_receipts.get(body['invocationId']))
            return {'state': 'succeeded', 'result': []}
        with patch('tangram_app.cli_hub.os_request', side_effect=server):
            status, result = run('call', 'g/a#Thing.List', '--input', '{}')
        self.assertEqual(status, 0, result)
        self.assertEqual(result['data']['target']['workspace'], 'demo')

    def test_remote_ambiguous_action_and_catalog_errors_do_not_dispatch(self):
        self.remote()
        cases = [({'candidates': [{'action': 'g/a#Thing.Do@one', 'contract': 'v1'}, {'action': 'g/a#Thing.Do@two', 'contract': 'v1'}]}, 'ambiguous_action'),
                 ({'error': {'code': 'permission_denied', 'message': 'No catalog access'}}, 'permission_denied')]
        for page, expected in cases:
            with self.subTest(expected=expected), patch('tangram_app.cli_hub.os_request', return_value=page) as request:
                status, result = run('call', 'g/a#Thing.Do', '--input', '{}')
            self.assertNotEqual(status, 0)
            self.assertEqual(result['error']['code'], expected)
            self.assertEqual(request.call_count, 1)

    def test_explicit_workspace_mutation_preserves_server_policy(self):
        self.remote()
        def server(cred, method, path, body, **kwargs):
            if path.endswith('/search'):
                return {'candidates': [{'action': 'g/a#Thing.Do@do', 'contract': 'digest'}]}
            self.assertTrue(path.endswith('/call'))
            self.assertFalse(body['approve'])
            self.assertFalse(body['allowMutation'])
            return {'state': 'rejected', 'error': {'code': 'policy_denied', 'message': 'No grant'}}
        with patch('tangram_app.cli_hub.os_request', side_effect=server):
            status, result = run('call', 'g/a#Thing.Do', '--workspace', 'dev/demo', '--input', '{}')
        self.assertNotEqual(status, 0)
        self.assertEqual(result['error']['code'], 'policy_denied')
        self.assertEqual(result['data']['target']['workspace'], 'demo')

    def test_receipt_failure_prevents_dispatch(self):
        self.local()
        with patch('tangram_app.hub_receipts.save', side_effect=OSError('disk full')), patch('tangram_app.desktop_invocation.call') as execute:
            status, result = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":1}')
        self.assertNotEqual(status, 0)
        execute.assert_not_called()
        # No receipt exists, so this must never be unknown/resumable.
        self.assertEqual(result['data']['state'], 'failed')
        self.assertEqual(result['error']['code'], 'receipt_unavailable')
        self.assertEqual(result['data']['retryMode'], 'new_invocation')
        self.assertNotIn('resumeCommand', result['data'])

    def test_interrupted_receipt_save_is_not_resumable(self):
        self.local()
        with patch('tangram_app.hub_receipts.save', side_effect=KeyboardInterrupt()), patch('tangram_app.desktop_invocation.call') as execute:
            status, result = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":1}')
        self.assertNotEqual(status, 0)
        execute.assert_not_called()
        self.assertEqual(result['data']['state'], 'failed')
        self.assertEqual(result['error']['code'], 'receipt_unavailable')
        self.assertEqual(result['data']['retryMode'], 'new_invocation')
        self.assertNotIn('resumeCommand', result['data'])

    def test_status_uses_receipt_even_when_context_changes(self):
        self.local()
        with patch('tangram_app.desktop_invocation.call', return_value={'state': 'succeeded'}):
            _, result = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":1}')
        key = result['data']['invocationId']
        with patch.dict(os.environ, {'TANGRAM_INSTANCE': 'other', 'TANGRAM_WORKSPACE': 'prod'}), patch('tangram_app.desktop_invocation.status', return_value={'state': 'succeeded', 'invocationId': key}):
            status, result = run('invocation', key)
        self.assertEqual(status, 0, result)
        self.assertEqual(result['data']['target']['lane'], 'local')

    def test_invalid_input_is_failed_before_dispatch_without_resume(self):
        self.local()
        with patch('tangram_app.desktop_invocation.call') as execute:
            status, result = run('call', 'com.example/orders#Order.Approve', '--input', '{"bad":1}')
        self.assertNotEqual(status, 0)
        self.assertEqual(result['data']['state'], 'failed')
        self.assertEqual(result['error']['code'], 'invalid_input')
        self.assertNotIn('resumeCommand', result['data'])
        execute.assert_not_called()

    def test_missing_desktop_is_failed_and_can_resume_after_open(self):
        self.local()
        status, result = run('call', 'com.example/orders#Order.Approve', '--input', '{"orderId":1}')
        self.assertNotEqual(status, 0)
        self.assertEqual(result['data']['state'], 'failed')
        self.assertEqual(result['error']['code'], 'approver_unavailable')
        key = result['data']['invocationId']
        with patch('tangram_app.desktop_invocation.call', return_value={'state': 'succeeded'}) as execute:
            status, result = run('resume', key)
        self.assertEqual(status, 0)
        self.assertEqual(execute.call_args.args[0].invocation_id, key)

    def test_failed_read_resume_returns_terminal_result_without_retry_claim(self):
        self.local()
        from tangram_app.cli_parser import build_parser
        from tangram_app.cli_hub import run as hub_run
        from tangram_app.local_runtime import LocalRuntimeError
        with patch('tangram_app.cli._run', side_effect=LocalRuntimeError('startup failed')) as execute:
            result = hub_run(build_parser().parse_args(['app', 'call', 'com.example/orders#Order.List', '--input', '{}']))
            resumed = hub_run(build_parser().parse_args(['app', 'resume', result['invocationId']]))
        self.assertEqual(execute.call_count, 1)
        for response in (result, resumed):
            self.assertEqual(response['state'], 'failed')
            self.assertFalse(response['error']['retryable'])
            self.assertEqual(response['retryMode'], 'new_invocation')
            self.assertNotIn('resumeCommand', response)

    def test_receipt_preserves_latest_explicit_consent_for_resume(self):
        self.remote()
        seen = []
        def server(cred, method, path, body, **kwargs):
            self.assertTrue(path.endswith('/call'))
            seen.append(body)
            return {'state': 'pending_approval'}
        with patch('tangram_app.cli_hub.os_request', side_effect=server):
            common = ['call', 'g/a#Thing.Do@do', '--contract', 'v1', '--workspace', 'dev/demo', '--input', '{}']
            _, result = run(*common)
            key = result['data']['invocationId']
            run(*common, '--invocation-id', key, '--allow-mutation', '--approve')
            run('resume', key)
        self.assertFalse(seen[0]['approve'])
        self.assertTrue(seen[1]['approve'])
        self.assertTrue(seen[2]['approve'])
        self.assertTrue(seen[2]['allowMutation'])
        self.assertEqual({body['invocationId'] for body in seen}, {key})

    def test_remote_catalog_shape_is_validated(self):
        self.remote()
        for page in ([], {}, {'candidates': None}, {'candidates': [None]},
                     {'candidates': [{'action': 'g/a#Thing.Do@do'}]},
                     {'candidates': [{'action': 'g/a#Thing.Do@do', 'contract': 3}]},
                     {'candidates': [], 'nextOffset': True}):
            with self.subTest(page=page), patch('tangram_app.cli_hub.os_request', return_value=page) as request:
                status, result = run('call', 'g/a#Thing.Do', '--input', '{}')
            self.assertNotEqual(status, 0)
            self.assertEqual(result['error']['code'], 'invalid_catalog')
            self.assertEqual(request.call_args.args[3]['query'], 'g/a#Thing.Do')

    def test_catalog_search_has_page_and_elapsed_time_limits(self):
        self.remote()
        def pages(cred, method, path, body, **kwargs):
            return {'candidates': [], 'nextOffset': body['offset'] + 100}
        with patch('tangram_app.cli_hub.os_request', side_effect=pages) as request:
            _, result = run('call', 'g/a#Thing.Do', '--input', '{}')
        self.assertEqual(request.call_count, 10)
        self.assertEqual(result['error']['code'], 'catalog_too_large')
        with patch('time.monotonic', side_effect=[0, 2]), patch('tangram_app.cli_hub.os_request') as request:
            _, result = run('call', 'g/a#Thing.Do', '--input', '{}', '--timeout', '1')
        self.assertEqual(result['error']['code'], 'catalog_timeout')
        request.assert_not_called()
