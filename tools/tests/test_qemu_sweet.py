"""Host-only regression tests for the QEMU evidence harness."""
import importlib.util
import io
import json
import os
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / 'qemu-sweet.py'
SPEC = importlib.util.spec_from_file_location('qemu_sweet', MODULE_PATH)
qemu_sweet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qemu_sweet)


class FakeProcess:
    def __init__(self, messages):
        self.stdout = io.StringIO(''.join(json.dumps(item) + '\n' for item in messages))
        self.stdin = io.StringIO()


class FramebufferTests(unittest.TestCase):
    def test_decodes_glyph_with_custom_geometry_and_stride(self):
        font = bytearray(256 * 16)
        font[ord('A') * 16] = 1
        raw = bytearray(16 * 32 * 4)
        raw[0:4] = b'\xff' * 4
        text, unknown = qemu_sweet.decode_screen(raw, bytes(font), 16, 32, 64)
        self.assertEqual(text, 'A\n')
        self.assertEqual(unknown, 0)

    def test_unknown_glyph_is_incomplete_not_success(self):
        raw = bytearray(16 * 32 * 4)
        raw[:4] = b'\xff' * 4
        text, unknown = qemu_sweet.decode_screen(raw, bytes(256 * 16), 16, 32, 64)
        result = qemu_sweet.screen_result(text, unknown)
        self.assertEqual(result['status'], 'incomplete_decode')
        self.assertFalse(result['full_boot_verified'])

    def test_truncated_framebuffer_and_bad_geometry_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, 'truncated'):
            qemu_sweet.decode_screen(b'\0' * 8, bytes(256 * 16), 16, 32, 64)
        with self.assertRaisesRegex(ValueError, 'geometry'):
            qemu_sweet.decode_screen(bytes(2048), bytes(256 * 16), 16, 32, 63)
        with self.assertRaisesRegex(ValueError, 'geometry'):
            qemu_sweet.decode_screen(bytes(2048), bytes(256 * 16), 16, 31, 64)

    def test_blank_and_panic_classification(self):
        blank = qemu_sweet.screen_result('   \n', 0)
        panic = qemu_sweet.screen_result('panic(cpu 0 caller 0x1): test\n', 0)
        self.assertEqual(blank['status'], 'blank_screen')
        self.assertEqual(panic['status'], 'panic')
        self.assertTrue(blank['snapshot_only'])
        self.assertFalse(panic['full_boot_verified'])

    def test_markers_do_not_upgrade_snapshot_to_full_boot(self):
        screen = 'VM bootstrap done\nSWEET AMFI: registered\n'
        result = qemu_sweet.screen_result(screen, 0)
        qemu_sweet.apply_expected_text(result, screen, ['VM bootstrap done', 'AMFI'])
        self.assertTrue(result['markers_satisfied'])
        self.assertEqual(result['missing_expected_text'], [])
        self.assertFalse(result['full_boot_verified'])

    def test_missing_expected_text_is_explicit(self):
        result = qemu_sweet.screen_result('platform expert registered\n', 0)
        qemu_sweet.apply_expected_text(result, 'platform expert registered\n', ['AMFI'])
        self.assertFalse(result['markers_satisfied'])
        self.assertEqual(result['missing_expected_text'], ['AMFI'])
        self.assertEqual(result['status'], 'no_panic_observed')


class QMPTests(unittest.TestCase):
    def test_stdio_client_skips_events_and_returns_matching_response(self):
        proc = FakeProcess([
            {'QMP': {'version': {}}},
            {'event': 'STOP'},
            {'return': {}, 'id': 1},
            {'return': 'VM status: paused', 'id': 2},
        ])
        client = qemu_sweet.QMPStdio(proc)
        self.assertEqual(qemu_sweet.qmp(client, 'info status'), ['VM status: paused'])
        calls = [json.loads(line) for line in proc.stdin.getvalue().splitlines()]
        self.assertEqual([call['execute'] for call in calls],
                         ['qmp_capabilities', 'human-monitor-command'])
        self.assertEqual(calls[1]['arguments']['command-line'], 'info status')

    def test_qmp_command_error_is_not_silently_returned_as_empty_output(self):
        proc = FakeProcess([
            {'QMP': {'version': {}}},
            {'return': {}, 'id': 1},
            {'error': {'class': 'CommandNotFound', 'desc': 'unknown'}, 'id': 2},
        ])
        client = qemu_sweet.QMPStdio(proc)
        with self.assertRaisesRegex(RuntimeError, 'QMP command'):
            qemu_sweet.qmp(client, 'not-a-command')

    def test_qmp_capability_error_fails_initialization(self):
        proc = FakeProcess([
            {'QMP': {'version': {}}},
            {'error': {'class': 'GenericError', 'desc': 'denied'}, 'id': 1},
        ])
        with self.assertRaisesRegex(RuntimeError, 'QMP command'):
            qemu_sweet.QMPStdio(proc)


if __name__ == '__main__':
    unittest.main()
