"""Проверки серверного установщика без Docker Engine и внешней сети."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
if os.name == 'nt':
    candidate = Path(r'C:\Program Files\Git\bin\bash.exe')
    BASH = str(candidate) if candidate.exists() else None
COMPOSE = shutil.which('docker-compose')
if os.name == 'nt':
    candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DockerDesktop/resources/bin/docker-compose.exe'
    COMPOSE = str(candidate) if candidate.exists() else None


@unittest.skipUnless(BASH, 'Нужен Bash для проверок установщика')
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='atlas-installer-')
        self.root = Path(self.folder.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('install.sh', 'compose.server.yaml', '.env.example'):
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / 'Dockerfile').write_text('FROM scratch\n', encoding='utf-8')
        (self.root / 'deploy').mkdir()
        shutil.copyfile(ROOT / 'deploy/Caddyfile', self.root / 'deploy/Caddyfile')
        self.log = self.root / 'docker-calls.txt'
        self.env = os.environ.copy()
        self.env.pop('DOCKER_HOST', None)
        self.env.pop('ATLAS_DOMAIN', None)
        self.env.update({'STUB_LOG': self.log.as_posix(), 'MSYS_NO_PATHCONV': '1'})
        bash_path = self.bin.as_posix()
        if os.name == 'nt':
            bash_path = '/' + bash_path[0].lower() + bash_path[2:]
        # Подмены находятся только во временной папке, Docker Engine не вызывается.
        self.shell_prefix = 'export PATH="' + bash_path + ':$PATH"; '
        self.stub('uname', 'printf "Linux\\n"\n')
        self.stub('docker', '''printf '%s\\n' "$*" >> "$STUB_LOG"
case "$1 $2" in
  'compose version') echo 5.5.1 ;;
  'context inspect') echo unix:///var/run/docker.sock ;;
  'info --format') echo linux ;;
  'ps -q') [[ "${STUB_OWN:-}" == yes ]] && echo existing-caddy; exit 0 ;;
  'volume ls') [[ "${STUB_VOLUMES:-}" == yes ]] && echo previous-server-data; exit 0 ;;
  compose*) [[ "${STUB_COMPOSE_FAIL:-}" != yes ]] ;;
  *) exit 1 ;;
esac
''')
        self.stub('getent', '[[ "${STUB_DNS_FAIL:-}" != yes ]] && echo "203.0.113.8 STREAM $2"\n')
        self.stub('ss', 'if [[ "${STUB_BUSY:-}" == yes ]]; then echo "LISTEN 0 128 0.0.0.0:443"; fi\n')
        self.stub('curl', '[[ "${STUB_HTTPS_FAIL:-}" != yes ]] && printf \'{"status":"ok"}\\n\'\n')
        self.stub('sleep', 'exit 0\n')

    def tearDown(self):
        self.folder.cleanup()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text('#!/usr/bin/env bash\n' + body, encoding='utf-8', newline='\n')
        path.chmod(0o755)

    def shell(self, script, *args, input_text=None):
        run_env = self.env.copy()
        for index, arg in enumerate(args):
            run_env[f'ATLAS_ARG_{index}'] = str(arg)
        arguments = ' '.join('"${ATLAS_ARG_' + str(index) + '}"' for index in range(len(args)))
        command = self.shell_prefix + 'set -- ' + arguments + '; ' + script
        result = subprocess.run([BASH, '-c', command, '_'],
                                input=input_text.encode('utf-8') if input_text is not None else None,
                                capture_output=True, env=run_env, timeout=25)
        result.stdout = result.stdout.decode('utf-8', errors='replace')
        result.stderr = result.stderr.decode('utf-8', errors='replace')
        return result

    def install(self, input_text='', *args):
        return self.shell('bash "$1" "${@:2}"', self.root / 'install.sh', *args, input_text=input_text)

    def source(self, command, *args):
        return self.shell('source "$1"; shift; ' + command, ROOT / 'install.sh', *args)

    def inputs(self, password='Test!pass$123'):
        return 'atlas.company.ru\nFAKE-test-key\n' + password + '\n' + password + '\n'

    def test_bash_syntax(self):
        result = subprocess.run([BASH, '-n', str(ROOT / 'install.sh')], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_help_has_no_side_effects(self):
        result = self.install('', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / '.env.server').exists())

    def test_domain_validation(self):
        for domain in ('atlas.company.ru', 'a-b.example.org', 'xn--e1afmkfd.xn--p1ai'):
            with self.subTest(domain=domain):
                self.assertEqual(self.source('valid_domain "$1"', domain).returncode, 0)
        for domain in ('localhost', '127.0.0.1', 'https://a.ru', 'a.ru/path', 'a..ru', '-a.ru',
                       'a-.ru', 'a.ru:443', 'a.ru\n{', 'a_b.ru', '.a.ru', 'a.ru.'):
            with self.subTest(domain=domain):
                self.assertNotEqual(self.source('valid_domain "$1"', domain).returncode, 0)

    def test_check_does_not_write_config_or_start_services(self):
        result = self.install('', '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / '.env.server').exists())
        self.assertNotIn('up -d', self.log.read_text())

    def test_busy_ports_stop_before_secrets_or_writes(self):
        self.env['STUB_BUSY'] = 'yes'
        result = self.install(self.inputs())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env.server').exists())
        self.assertNotIn('up -d', self.log.read_text())

    def test_unresolved_domain_does_not_write_secrets(self):
        self.env['STUB_DNS_FAIL'] = 'yes'
        result = self.install(self.inputs())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env.server').exists())

    def test_password_mismatch_does_not_create_config(self):
        result = self.install('atlas.company.ru\nFAKE-key\nPassword123\nOtherPassword\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env.server').exists())

    def test_short_password_does_not_create_config(self):
        result = self.install(self.inputs('short'))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env.server').exists())

    def test_install_is_isolated_and_secrets_are_not_printed(self):
        local = self.root / '.env'
        local.write_text('LOCAL_VALUE=keep-me\n', encoding='utf-8')
        result = self.install(self.inputs())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(local.read_text(), 'LOCAL_VALUE=keep-me\n')
        server = (self.root / '.env.server').read_text(encoding='utf-8')
        self.assertIn('SESSION_COOKIE_SECURE=true', server)
        for secret in ('FAKE-test-key', 'Test!pass$123'):
            self.assertNotIn(secret, result.stdout + result.stderr + self.log.read_text())
        calls = self.log.read_text()
        self.assertIn('-p zag-atlas-server', calls)
        self.assertIn('compose.server.yaml', calls)
        self.assertNotIn(' -f compose.yaml', calls)
        self.assertIn('up -d --build --wait', calls)
        self.assertFalse(list(self.root.glob('.env.server.tmp.*')))

    def test_existing_server_env_is_reused_without_input(self):
        path = self.root / '.env.server'
        original = "ATLAS_DOMAIN=atlas.company.ru\nPROXYAPI_API_KEY=keep-key\nADMIN_PASSWORD=keep-pass\n"
        path.write_text(original, encoding='utf-8')
        self.env.update({'STUB_BUSY': 'yes', 'STUB_OWN': 'yes'})
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_invalid_existing_domain_is_rejected(self):
        path = self.root / '.env.server'
        path.write_text('ATLAS_DOMAIN=bad/path\n', encoding='utf-8')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('up -d', self.log.read_text())

    def test_compose_failure_preserves_config_for_retry(self):
        self.env['STUB_COMPOSE_FAIL'] = 'yes'
        result = self.install(self.inputs())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / '.env.server').exists())
        self.assertNotIn('up -d', self.log.read_text())

    def test_existing_data_without_settings_are_not_reused(self):
        self.env['STUB_VOLUMES'] = 'yes'
        result = self.install(self.inputs())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env.server').exists())
        self.assertNotIn('up -d', self.log.read_text())

    def test_remote_docker_is_rejected(self):
        self.env['DOCKER_HOST'] = 'tcp://other-server:2376'
        result = self.install(self.inputs())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / '.env.server').exists())

    def test_https_failure_is_not_reported_as_success(self):
        self.env['STUB_HTTPS_FAIL'] = 'yes'
        result = self.source('wait_for_https "$1" 2 0', 'atlas.company.ru')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('HTTPS', result.stderr)

    @unittest.skipUnless(COMPOSE, 'Нужен Compose CLI для проверки парсера без Engine')
    def test_compose_parser_and_secret_roundtrip(self):
        key = "FAKE-$key#quote'back\\slash"
        password = "P@ss word$#'\\end"
        result = self.source('render_env "$1" "$2" "$3" "$4"',
                             self.root / '.env.example', 'atlas.company.ru', key, password)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.root / '.env.server'
        path.write_text(result.stdout, encoding='utf-8')
        domain_file = self.root / 'deploy/server-domain.env'
        domain_file.write_text('ATLAS_DOMAIN=atlas.company.ru\n', encoding='utf-8')
        result = subprocess.run([COMPOSE, '--env-file', str(domain_file), '-f', str(self.root / 'compose.server.yaml'),
                                 '-p', 'zag-atlas-server', 'config', '--format', 'json'],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        atlas = config['services']['atlas']
        # config экранирует $ как $$ при сериализации модели для повторного чтения.
        self.assertEqual(atlas['environment']['PROXYAPI_API_KEY'], key.replace('$', '$$'))
        self.assertEqual(atlas['environment']['ADMIN_PASSWORD'], password.replace('$', '$$'))
        self.assertEqual(atlas['environment']['SESSION_COOKIE_SECURE'], 'true')
        self.assertFalse(atlas.get('ports'))
        self.assertFalse(config['services']['qdrant'].get('ports'))
        self.assertEqual(config['name'], 'zag-atlas-server')
        self.assertTrue(all(v['name'].startswith('zag-atlas-server_') for v in config['volumes'].values()))

    @unittest.skipUnless(COMPOSE, 'Нужен Compose CLI для проверки парсера без Engine')
    def test_raw_password_edge_cases(self):
        domain_file = self.root / 'deploy/server-domain.env'
        domain_file.write_text('ATLAS_DOMAIN=atlas.company.ru\n', encoding='utf-8')
        for password in ('Ends-with-backslash\\', ' leading and trailing ', 'Quotes\'"$#inside'):
            with self.subTest(password_kind='спецсимволы'):
                rendered = self.source('render_env "$1" "$2" "$3" "$4"',
                                       self.root / '.env.example', 'atlas.company.ru', 'FAKE-key', password)
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                (self.root / '.env.server').write_text(rendered.stdout, encoding='utf-8')
                result = subprocess.run([COMPOSE, '--env-file', str(domain_file), '-f', str(self.root / 'compose.server.yaml'),
                                         '-p', 'zag-atlas-server', 'config', '--format', 'json'],
                                        capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)['services']['atlas']['environment']['ADMIN_PASSWORD']
                self.assertEqual(value, password.replace('$', '$$'))


if __name__ == '__main__':
    unittest.main()
