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
            return {'invocationId':key, **states.pop(0)}
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

    def test_reads_and_terminal_replays_never_prompt(self):
        result,calls,prompt = self.pipeline(lambda *a,**k: self.fail('unexpected popup'), [{'state':'succeeded'}])
        prompt.assert_not_called()
        self.assertEqual(len(calls),1)

if __name__ == '__main__': unittest.main()
