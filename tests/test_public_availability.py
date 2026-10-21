"""Offline checks: subprocess transport is mocked; no DB/browser/network."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch

from scripts import check_public_availability as checker


def page(path='/best-apartments'):
    return f'<html><title>Обзор — Hatuli</title><h1>{checker.HEADINGS[path]}</h1></html>'.encode()


class PublicAvailabilityTests(unittest.TestCase):
    def test_probe_headings_match_the_current_public_templates(self):
        root=Path(__file__).resolve().parents[1]/'bot'/'templates'
        for route, filename in (('/best-apartments','daily_picks.html'),
                                ('/best-rentals','rental_picks.html'),
                                ('/analytics','analytics_articles.html')):
            template=(root/filename).read_text(encoding='utf-8')
            self.assertIn('Hatuli',template)
            self.assertEqual(checker.classify(route,200,('<title>Hatuli</title>'+template).encode()),'ok')

    def test_route_shape_confirms_all_three_public_html_routes(self):
        for path in checker.HEADINGS:
            with self.subTest(path=path):
                self.assertEqual(checker.classify(path, 200, page(path)), 'ok')

    def test_200_without_expected_route_is_not_a_success(self):
        for body in (b'', b'<title>Login</title>', b'<h1>Hatuli</h1>',
                     b'<title>Hatuli</title><h1>Sign in</h1>', page('/best-rentals')):
            with self.subTest(body=body):
                self.assertEqual(checker.classify('/best-apartments', 200, body), 'unexpected_content')

    def test_plain_and_html_cloudflare_1033_are_distinguished(self):
        for body in (b'error code: 1033', b'<h1><span>Error</span><span>1033</span></h1>'):
            self.assertEqual(checker.classify('/analytics', 530, body), 'tunnel_error_1033')

    def test_a_1033_reference_in_a_healthy_page_is_not_an_outage(self):
        self.assertEqual(checker.classify('/analytics', 200, page('/analytics') + b'Error 1033'), 'ok')

    def test_blocks_do_not_claim_the_origin_is_down(self):
        for status, body in ((403, b''), (429, b''), (200, b'<title>Just a moment...</title>')):
            self.assertEqual(checker.classify('/analytics', status, body), 'access_blocked')

    def test_redirect_and_other_http_errors_fail_closed(self):
        for status, outcome in ((301, 'unexpected_redirect'), (302, 'unexpected_redirect'),
                                (404, 'http_error'), (500, 'http_error'), (530, 'http_error')):
            self.assertEqual(checker.classify('/analytics', status, b'other'), outcome)

    def test_sitemap_requires_namespace_urlset_and_known_canonical_route(self):
        xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'
               + checker.PUBLIC_BASE + '/analytics</loc></url></urlset>').encode()
        self.assertEqual(checker.classify('/sitemap.xml', 200, xml), 'ok')
        for body in (b'<urlset/>', b'<urlset', b'<html>Hatuli</html>',
                     xml.replace(checker.PUBLIC_BASE.encode(), b'https://wrong.invalid')):
            self.assertEqual(checker.classify('/sitemap.xml', 200, body), 'unexpected_content')

    def test_oversized_body_cannot_pass_content_validation(self):
        self.assertEqual(checker.classify('/analytics', 200, b'x'*(checker.MAX_BYTES+1)), 'unexpected_content')

    def test_probe_is_bounded_and_does_not_follow_redirects_or_save_raw_receipt(self):
        paths = []
        def run(args, **kwargs):
            paths.append(Path(args[args.index('--output') + 1]))
            paths[-1].write_bytes(page())
            self.assertEqual(kwargs['timeout'], 10)
            self.assertEqual(args[1], '--disable')
            self.assertIn('--max-filesize', args)
            self.assertIn('--max-time', args)
            self.assertNotIn('--location', args)
            self.assertNotIn('--insecure', args)
            self.assertEqual(args[-1], checker.PUBLIC_BASE + '/best-apartments')
            return SimpleNamespace(stdout=b'200', stderr=b'PRIVATE-DATA', returncode=0)
        with patch.object(checker.subprocess, 'run', side_effect=run):
            result = checker.probe('/best-apartments')
        self.assertEqual(result['outcome'], 'ok')
        self.assertEqual(result['http_status'], 200)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertFalse(paths[0].exists())

    def test_transport_failure_categories_never_confirm_availability(self):
        for code, outcome in ((6,'dns_error'), (7,'connection_error'), (28,'timeout'),
                              (35,'tls_error'), (60,'tls_error'), (63,'body_too_large'), (18,'transport_error')):
            with self.subTest(code=code), patch.object(checker.subprocess, 'run', return_value=
                    SimpleNamespace(stdout=b'000', stderr=b'PRIVATE', returncode=code)):
                result = checker.probe('/analytics')
                self.assertEqual(result['outcome'], outcome)
                self.assertIsNone(result['http_status'])
                self.assertNotIn('PRIVATE', json.dumps(result))

    def test_invalid_curl_status_is_not_healthy(self):
        with patch.object(checker.subprocess, 'run', return_value=
                          SimpleNamespace(stdout=b'not-a-status', stderr=b'', returncode=0)):
            self.assertEqual(checker.probe('/analytics')['outcome'], 'transport_error')

    def test_timeout_and_missing_binary_have_distinct_outcomes(self):
        for error, outcome in ((subprocess.TimeoutExpired('curl',10),'timeout'),
                               (FileNotFoundError('curl'),'checker_error')):
            with patch.object(checker.subprocess, 'run', side_effect=error):
                self.assertEqual(checker.probe('/analytics')['outcome'], outcome)

    def test_fixed_routes_prevent_arbitrary_requests(self):
        with patch.object(checker.subprocess, 'run') as run:
            for path,target in (('https://wrong.invalid','public'), ('/analytics','wrong'), ('/admin','public')):
                with self.assertRaises(ValueError):
                    checker.probe(path,target=target)
            run.assert_not_called()

    def test_local_target_is_explicit_loopback_only(self):
        with patch.object(checker.subprocess, 'run', return_value=
                          SimpleNamespace(stdout=b'302',stderr=b'',returncode=0)) as run:
            result=checker.probe('/analytics',target='local')
        self.assertEqual(run.call_args.args[0][-1], checker.LOCAL_BASE+'/analytics')
        self.assertEqual(result['outcome'], 'unexpected_redirect')

    def test_report_is_not_a_claim_of_fresh_editions_and_checks_every_route(self):
        calls=[]
        def fetch(path,**kwargs):
            calls.append(path)
            return {'path':path,'http_status':200,'outcome':'ok','duration_ms':1}
        receipt=checker.report(fetch=fetch)
        self.assertTrue(receipt['all_routes_confirmed'])
        self.assertEqual(receipt['scope'],'availability_only')
        self.assertEqual(tuple(calls),checker.ROUTES)
        self.assertNotIn('users',receipt)

    def test_cli_exit_is_nonzero_for_partial_or_blocked_results(self):
        for healthy in (True,False):
            with patch.object(checker,'report',return_value={'all_routes_confirmed':healthy}), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(checker.main([]),0 if healthy else 1)
            self.assertEqual(json.loads(out.getvalue())['all_routes_confirmed'],healthy)


if __name__ == '__main__':
    unittest.main()
