import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch

from transcription.pipeline import TranscriptionPipeline
from transcription.runner import JobRunner
from transcription.db import JobStore
from transcription.settings import Settings


class CustomPromptTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / 'config.yml'
        config.write_text('{}')
        self.settings = replace(Settings.load(config), data_dir=self.root,
                                analysis_prompt='Configured instructions')
        self.pipeline = TranscriptionPipeline(self.settings, Mock(), Mock(), Event())
        self.client = Mock()
        self.client.chat.completions.create.return_value = [
            Mock(choices=[Mock(delta=Mock(content='Analysis results'))]),
        ]
        self.pipeline._client = Mock(return_value=self.client)

    def test_custom_prompt_replaces_placeholders_without_configured_instructions(self):
        transcript = 'Alice: C:\\notes\\1 {meeting_transcript}'
        state = dict(job_dir=str(self.root), transcript=transcript,
                     custom_prompt='Summarize { meeting_transcript } / {meeting_transcript}; {"json": true}',
                     brief_description='Background excluded from custom prompt')
        self.pipeline.run_analysis_only(state)
        fenced = f'\n```\n{transcript}\n```\n'
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs['messages'], [
            {'role': 'user', 'content': f'Summarize {fenced} / {fenced}; {{"json": true}}'},
        ])
        self.assertEqual((self.root / 'summary.txt').read_text(), 'Analysis results')
        self.client.close.assert_called_once()

    def test_multiline_transcript_with_code_fences_stays_inside_wrapper(self):
        transcript = 'Alice:\n```python\nprint("hello")\n```\nBob: Done.'
        self.pipeline.run_analysis_only(dict(
            job_dir=str(self.root), transcript=transcript,
            custom_prompt='Before { meeting_transcript } After',
        ))
        self.assertEqual(self.client.chat.completions.create.call_args.kwargs['messages'], [
            {'role': 'user', 'content': f'Before \n````\n{transcript}\n````\n After'},
        ])

    def test_default_prompt_still_includes_configured_instructions_and_context(self):
        self.pipeline.run_analysis_only(dict(job_dir=str(self.root), transcript='Transcript',
                                            brief_description='Background'))
        messages = self.client.chat.completions.create.call_args.kwargs['messages']
        self.assertTrue(messages[0]['content'].startswith('Configured instructions'))
        self.assertIn('Background', messages[1]['content'])
        self.assertIn('Transcript', messages[1]['content'])

    def test_runner_passes_template_on_initial_run_and_regeneration(self):
        store = JobStore(self.root / 'jobs.sqlite')
        store.initialize()
        store.create('job', 'Meeting', 'voice', [], {
            'custom_prompt': 'Actions: { meeting_transcript }', 'auto_analysis': True,
        })
        job_dir = self.settings.job_dir('job')
        job_dir.mkdir(parents=True)
        (job_dir / 'results.txt').write_text('Edited transcript')
        runner = JobRunner(self.settings, store, on_stopped=Mock())
        self.addCleanup(runner.pool.shutdown)
        with patch('transcription.runner.TranscriptionPipeline') as pipeline:
            runner._run('job', Event())
            initial = pipeline.return_value.run.call_args.args[0]
            self.assertEqual(initial['custom_prompt'], 'Actions: { meeting_transcript }')
            self.assertTrue(initial['auto_analysis'])
            runner._run_analysis('job', Event())
            regenerated = pipeline.return_value.run_analysis_only.call_args.args[0]
            self.assertEqual(regenerated['custom_prompt'], initial['custom_prompt'])
            self.assertEqual(regenerated['transcript'], 'Edited transcript')
        self.assertEqual(store.get('job')['status'], 'completed')
