"""Revision dispatches retain human feedback and create distinct snapshots."""
from tests.backend.test_dispatch import DispatchTests


class RevisionDispatchTests(DispatchTests):
    def test_repeated_same_rejection_binds_a_new_review_identity(self):
        self.brief()
        original = self.service.create_task_dispatch(self.item['id'], {
            'agent_id': self.agent['id'], 'directory_id': 'main',
            'expected_brief_revision': 1, 'confirmed': True})['dispatch']
        snapshots = []
        for verdict in ('changes_requested', 'rejected'):
            current = self.profile.store.get_item(self.item['id'])
            claimed = self.service.claim_agent_task(self.agent['id'], current['id'], current['revision'])
            submitted = self.service.submit_agent_review(self.agent['id'], current['id'],
                claimed['item']['revision'], 'First output', [], run_id=claimed['run']['id'])
            reviewed = self.service.save_review({'task_id': current['id'],
                'expected_revision': submitted['item']['revision'], 'verdict': verdict,
                'reason': '请补充实际工具结果', 'confirmed': True})
            preview = self.service.preview_task_dispatch(current['id'], {
                'agent_id': self.agent['id'], 'directory_id': 'main', 'delivery_mode': 'auto'})
            self.assertEqual(preview['dispatch']['revision_request']['id'], reviewed['review']['id'])
            self.assertIn('请补充实际工具结果', preview['content_text'])
            saved = self.service.create_task_dispatch(current['id'], {
                'agent_id': self.agent['id'], 'directory_id': 'main', 'delivery_mode': 'auto',
                'expected_brief_revision': 1, 'preview_sha256': preview['brief_sha256'], 'confirmed': True})
            snapshots.append(saved['dispatch'])
        self.assertNotEqual(snapshots[0]['id'], snapshots[1]['id'])
        self.assertNotEqual(snapshots[0]['brief_sha256'], snapshots[1]['brief_sha256'])
        self.assertNotIn('Human revision request', original['content_text'])
        self.assertEqual(self.service.task_dispatch(original['id'])['content_text'], original['content_text'])
