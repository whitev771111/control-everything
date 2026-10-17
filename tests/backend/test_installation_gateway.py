"""Gateway installation assembly with fake server/observer/OS; no listeners."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from control_everything.http import gateway


class InstallationGatewayTests(unittest.TestCase):
    def server(self):
        server = object.__new__(gateway.PanelServer)
        server.observer = Mock()
        server.profile = SimpleNamespace(data_dir=Path('C:/example/fixture/profile'))
        server.serve_forever = Mock(side_effect=KeyboardInterrupt)
        return server

    def test_product_registers_inside_mutex_before_start_and_closes_own_lease(self):
        server, events = self.server(), []
        context = SimpleNamespace(install_root=Path('C:/example/fixture/install'))
        lease = Mock()
        api = Mock()
        @contextmanager
        def mutex(root):
            events.append('lock')
            try: yield
            finally: events.append('unlock')
        api.install_mutex = mutex
        def register(*args, **kwargs):
            self.assertEqual(kwargs, {'api': api})
            events.append('register')
            return lease
        server.observer.start.side_effect = lambda: events.append('start')
        with patch.object(gateway, 'product_version_dir', return_value=Path('C:/example/fixture/install/versions/v')), \
                patch.object(gateway.ProductContext, 'load', return_value=context), \
                patch.object(gateway, 'default_api', return_value=api), \
                patch.object(gateway, 'register_http', side_effect=register), \
                patch.object(gateway.ThreadingHTTPServer, 'server_close'):
            server.start_observer()
            self.assertEqual(events, ['lock', 'register', 'start', 'unlock'])
            server.server_close()
            server.server_close()
        lease.close.assert_called_once_with()

    def test_source_and_ordinary_wheel_skip_installation_registry(self):
        server = self.server()
        with patch.object(gateway, 'product_version_dir', return_value=None), \
                patch.object(gateway, 'default_api') as api, patch.object(gateway, 'register_http') as register:
            server.start_observer()
        server.observer.start.assert_called_once_with()
        api.assert_not_called(); register.assert_not_called()

    def test_bad_basic_closure_after_bind_closes_server_without_registry_or_spawn(self):
        server = self.server()
        with patch.object(gateway, 'build_profile_server', return_value=server), \
                patch.object(gateway, 'product_version_dir', return_value=Path('C:/example/fixture/product')), \
                patch.object(gateway.ProductContext, 'load', side_effect=gateway.ManifestError('asset_mismatch')), \
                patch.object(gateway, 'default_api') as api, \
                patch.object(gateway.ThreadingHTTPServer, 'server_close') as close, patch('sys.stderr'):
            self.assertEqual(gateway.serve(), 1)
        api.assert_not_called()
        server.observer.start.assert_not_called()
        server.serve_forever.assert_not_called()
        close.assert_called_once_with()

    def test_bind_failure_does_not_register_and_start_failure_cleans_lease(self):
        with patch.object(gateway, 'build_profile_server', side_effect=OSError('bind_failed')), \
                patch.object(gateway, 'register_http') as register, patch('sys.stderr'):
            self.assertEqual(gateway.serve(), 1)
        register.assert_not_called()
        server, lease = self.server(), Mock()
        def failed_start():
            server._installation_lease = lease
            raise OSError('start_failed')
        with patch.object(gateway, 'build_profile_server', return_value=server), \
                patch.object(server, 'start_observer', side_effect=failed_start), \
                patch.object(gateway.ThreadingHTTPServer, 'server_close'), patch('sys.stderr'):
            self.assertEqual(gateway.serve(), 1)
        lease.close.assert_called_once_with()

    def test_registration_failure_never_starts_observer_or_keeps_mutex(self):
        server, events = self.server(), []
        @contextmanager
        def mutex(root):
            events.append('lock')
            try: yield
            finally: events.append('unlock')
        api = SimpleNamespace(install_mutex=mutex)
        with patch.object(gateway, 'product_version_dir', return_value=Path('C:/example/fixture/version')), \
                patch.object(gateway.ProductContext, 'load', return_value=SimpleNamespace(install_root=Path('C:/example/fixture/install'))), \
                patch.object(gateway, 'default_api', return_value=api), \
                patch.object(gateway, 'register_http', side_effect=ValueError('root_identity')), \
                self.assertRaises(ValueError):
            server.start_observer()
        self.assertEqual(events, ['lock', 'unlock'])
        server.observer.start.assert_not_called()
