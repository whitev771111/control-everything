"""Repeated dispatch selection must retain one snapshot and its selected agent."""
import unittest

from control_everything.input_validation import ProfileError
from control_everything.workspace import agent_runs, dispatch
from tests.backend import test_dispatch as fixtures


class DispatchReselectionTests(unittest.TestCase):
    def setUp(self):
        fixtures.DispatchTests.setUp(self)
        current = self.profile.agent_settings()
        self.agents = self.profile.save_agents({'expected_revision': current['revision'],
            'items': [{**current['items'][0], 'adapter': 'codex'},
                      fixtures.agent(name='Synthetic B', adapter='codex')]})['items']
        fixtures.DispatchTests.brief(self)

    def select(self, agent, mode):
        return dispatch.create_dispatch(self.profile, self.item['id'], {
            'agent_id': agent['id'], 'directory_id': 'main', 'expected_brief_revision': 1,
            'confirmed': True, 'delivery_mode': mode})

    def test_reselecting_previous_auto_dispatch_synchronizes_assignment_and_is_idempotent(self):
        self.check_reselection('auto')

    def test_reselecting_previous_manual_dispatch_synchronizes_assignment_and_is_idempotent(self):
        self.check_reselection('manual')

    def check_reselection(self, mode):
        first = self.select(self.agents[0], mode)
        self.select(self.agents[1], mode)
        before = self.profile.store.get_item(self.item['id'])
        again = self.select(self.agents[0], mode)
        item = self.profile.store.get_item(self.item['id'])
        self.assertEqual((again['status'], again['dispatch']['id']), ('existing', first['dispatch']['id']))
        self.assertEqual(item['assigned_agent_id'], self.agents[0]['id'])
        self.assertEqual(item['revision'], before['revision'] + 1)
        facts = dispatch.execution_facts(self.profile, self.profile.store, again['dispatch']['id'])
        self.assertEqual(facts['task']['assigned_agent_id'], facts['dispatch']['agent_id'])
        repeated = self.select(self.agents[0], mode)
        self.assertEqual(repeated, again)
        self.assertEqual(self.profile.store.get_item(self.item['id']), item)
        self.assertEqual(len(dispatch.list_dispatches(self.profile.store, self.item['id'])['items']), 2)

    def test_reselection_does_not_override_an_active_agent_run(self):
        first = self.select(self.agents[0], 'manual')
        second = self.select(self.agents[1], 'manual')
        dispatch.mark_sent(self.profile.store, second['dispatch']['id'])
        item = self.profile.store.get_item(self.item['id'])
        claimed = agent_runs.claim(self.profile.store, self.agents[1]['id'], item['id'], item['revision'])
        before = self.profile.store.get_item(item['id'])
        with self.assertRaises(ProfileError) as caught:
            self.select(self.agents[0], 'manual')
        self.assertEqual(caught.exception.reason, 'run_conflict')
        self.assertEqual(self.profile.store.get_item(item['id']), before)
        self.assertEqual(dispatch.get_dispatch(self.profile.store, second['dispatch']['id'])['run_id'], claimed['run']['id'])
        self.assertIsNone(dispatch.get_dispatch(self.profile.store, first['dispatch']['id'])['run_id'])
