import unittest
from argparse import Namespace
from unittest.mock import patch
from tangram_app.hub_target import HubError
from tangram_app.workspace_approval import origin
from tangram_app.cli_hub import _run_pinned

class WorkspaceApprovalTest(unittest.TestCase):
    def test_origin_boundaries(self):
        self.assertEqual(origin('http://localhost:8081'), 'http://127.0.0.1:8081')
        self.assertEqual(origin('http://[::1]:8081'), 'http://[::1]:8081')
        self.assertEqual(origin('https://example.test:443/'), 'https://example.test')
        for url in ['http://localhost', 'http://127.0.0.2:80', 'http://example.test:8081',
                    'http://127.0.0.1.evil.test:80', 'http://user@127.0.0.1:80',
                    'https://example.test/path', 'https://example.test?x', 'file:///tmp/x']:
            with self.subTest(url=url), self.assertRaises(HubError): origin(url)

    def pipeline(self, native, states):
        import uuid
        key = str(uuid.uuid4())
        args = Namespace(capability_command='call', target_ref='ref', timeout=2,
                         invocation_id=key, action='g/a#Order.Approve', contract='digest',
                         input_json='{"orderId": 1}', command='app')
        target = {'workspace':'demo', 'endpoint':'http://localhost:8081'}
        calls = []
        def server(cred, method, path, body=None, **kw):
            calls.append((method,path,body))
            value = states.pop(0)
            if isinstance(value, Exception): raise value
            return {'invocationId':key, **value}
        with patch('tangram_app.cli_hub.load', return_value=(target, {'token':'shared'})), \
             patch('tangram_app.cli_hub.os_request', side_effect=server), \
             patch('tangram_app.workspace_approval.request', side_effect=native) as prompt:
            result = _run_pinned(args)
        return result, calls, prompt

    def test_approve_waits_and_executes_exact_frozen_request_once(self):
        result, calls, prompt = self.pipeline(lambda *a,**k: {'state':'approved'},
            [{'state':'pending_approval'}, {'state':'approved'}, {'state':'succeeded', 'result':42}])
        self.assertEqual(result['result'],42)
        self.assertEqual([c[0] for c in calls],['POST','GET','POST'])
        self.assertEqual(calls[0][2],calls[2][2])
        self.assertNotIn('approve',calls[0][2])
        prompt.assert_called_once()

    def test_fake_native_approval_cannot_trigger_dispatch(self):
        result, calls, _ = self.pipeline(lambda *a,**k: {'state':'approved'},
            [{'state':'pending_approval'}, {'state':'pending_approval'}])
        self.assertEqual(result['state'],'pending_approval')
        self.assertEqual(len(calls),2)

    def test_response_loss_reconciles_and_does_not_abandon_valid_decision(self):
        result, calls, _ = self.pipeline(HubError('approver_unavailable','response lost'),
            [{'state':'pending_approval'}, {'state':'approved'}, {'state':'succeeded'}])
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(calls[0][2],calls[2][2])

    def test_deny_expiry_or_unavailable_never_dispatch(self):
        for state in ['rejected','expired','pending_approval']:
            with self.subTest(state=state):
                result,calls,_ = self.pipeline(lambda *a,**k: {'error':{'code':'approver_unavailable','message':'no device'}},
                    [{'state':'pending_approval'}, {'state':state}])
                self.assertEqual(result['state'],state)
                self.assertEqual(len(calls),2)
                if state == 'pending_approval': self.assertEqual(result['error']['code'],'approver_unavailable')

    def test_invalid_target_is_not_resumable(self):
        # A frozen endpoint that can never reach an approver must reconcile,
        # not stay pending with a resume that repeats the same broken target.
        result,calls,_ = self.pipeline(HubError('invalid_target', 'endpoint carries a path'),
            [{'state':'pending_approval'}, {'state':'pending_approval'}])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['retryMode'],'reconcile')
        self.assertEqual(result['error']['code'],'invalid_target')
        self.assertFalse(result['error']['retryable'])
        self.assertEqual(len(calls),2)

    def test_failed_reconciliation_retains_native_expiry_without_claiming_server_expiry(self):
        native_error = {'code':'approval_expired','message':'Challenge expired','retryable':False}
        result,calls,_ = self.pipeline(lambda *a,**k: {'state':'expired','error':native_error},
            [{'state':'pending_approval'}, OSError('status unavailable')])
        self.assertEqual(result['state'],'unknown')
        self.assertEqual(result['error']['code'],'approval_status_unavailable')
        self.assertEqual(result['approvalError'],native_error)
        self.assertEqual(len(calls),2)

    def test_reads_and_terminal_replays_never_prompt(self):
        result,calls,prompt = self.pipeline(lambda *a,**k: self.fail('unexpected popup'), [{'state':'succeeded'}])
        prompt.assert_not_called()
        self.assertEqual(len(calls),1)


    def test_missing_enrollment_opens_setup_without_dispatching(self):
        result, calls, prompt = self.pipeline(lambda *a, **kw: {'state': 'enrollment_created'},
            [{'state': 'failed', 'setupRequired': True,
              'error': {'code': 'approver_unavailable', 'retryable': True}}])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['approvalSetup']['state'], 'enrollment_created')
        self.assertTrue(prompt.call_args.kwargs['setup'])
        self.assertEqual(len(calls), 1)

    def test_setup_failure_preserves_original_server_error(self):
        result, calls, _ = self.pipeline(HubError('approver_unavailable', 'Desktop unavailable'),
            [{'state': 'failed', 'setupRequired': True,
              'error': {'code': 'approver_unavailable', 'message': 'Registry not configured'}}])
        self.assertEqual(result['error']['message'], 'Registry not configured')
        self.assertEqual(result['approvalError']['message'], 'Desktop unavailable')
        self.assertEqual(len(calls), 1)

    def test_older_server_missing_registry_also_opens_setup(self):
        result, calls, prompt = self.pipeline(lambda *a, **kw: {'state': 'enrollment_created'},
            [{'state': 'rejected', 'error': {'code': 'approver_unavailable', 'retryable': False}}])
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['retryMode'], 'resume')
        self.assertTrue(result['error']['retryable'])
        self.assertTrue(prompt.call_args.kwargs['setup'])
        self.assertEqual(len(calls), 1)

if __name__ == '__main__': unittest.main()
