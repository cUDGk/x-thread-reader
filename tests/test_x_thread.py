import contextlib
import importlib.util
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('x_thread', ROOT / 'x_thread.py')
x = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x)
FIX = ROOT / 'tests' / 'fixtures'
PHOTO = '1900000000000000002'
VIDEO = '1900000000000000003'
CARD = '1900000000000000001'
ARTICLE = '1900000000000000004'


def fixture(prefix, ident):
    return json.loads((FIX / f'{prefix}_{ident}.json').read_text(encoding='utf-8'))


class TestReader(unittest.TestCase):
    def test_targets(self):
        for url in [
            'https://x.com/a/status/12345', 'https://twitter.com/a/statuses/12345',
            'https://mobile.twitter.com/a/status/12345', 'https://fxtwitter.com/a/status/12345',
            'https://api.fxtwitter.com/a/status/12345', 'https://vxtwitter.com/a/status/12345',
            'https://publish.twitter.com/oembed?url=https%3A%2F%2Fx.com%2Fa%2Fstatus%2F12345',
        ]:
            self.assertEqual(x.status_target(url), ('a', '12345'))
        self.assertEqual(x.status_target('12345'), (None, '12345'))
        self.assertIsNone(x.status_target('bad'))

    def test_http_errors(self):
        for code in (404, 503):
            err = urllib.error.HTTPError('https://example.com', code, 'error', {}, None)
            stderr = io.StringIO()
            with patch('urllib.request.urlopen', side_effect=err), contextlib.redirect_stderr(stderr):
                self.assertEqual(x.Syndication().fetch('12345'), {'__status': code})
            if code == 404:
                self.assertEqual(stderr.getvalue(), '')
            else:
                self.assertIn('warning: syndication HTTP 503', stderr.getvalue())

    def test_incomplete_read(self):
        with patch('urllib.request.urlopen', side_effect=http.client.IncompleteRead(b'')), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertIsNone(x.Syndication().fetch('12345'))
            self.assertEqual(x.fetch_page('12345', 'a'), '')
            self.assertIsNone(x.fetch_fx('a', '12345'))
        self.assertEqual(stderr.getvalue().count('warning:'), 3)

    def test_media_incomplete_read(self):
        p = {'id': '12345', 'media': [{'type': 'photo', 'url': 'https://pbs.twimg.com/media/a'}]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(x, 'download_bytes', side_effect=http.client.IncompleteRead(b'')), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            x.save_media([p], directory)
            self.assertIn('warning: media download failed:', stderr.getvalue())
            self.assertNotIn('file', p['media'][0])

    def test_long_form_notice(self):
        t = x.Thread(PHOTO)
        p = x.to_tweet(fixture('synd', PHOTO))
        t.posts = [p]
        for kind in ('is_note', 'is_article'):
            p[kind] = True
            p['full_text'] = None
            self.assertIn('long-form post: text may be truncated', x.render(t))
            p['full_text'] = 'complete'
            self.assertNotIn('long-form post', x.render(t))
            p[kind] = False

    def test_missing_mp4(self):
        p = {'id': '12345', 'media': [{'type': 'video', 'poster': 'https://pbs.twimg.com/media/a',
                                       'mp4_small': None}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(x, 'download_bytes', return_value=b'image'), \
                patch.object(x, 'make_frames') as frames, contextlib.redirect_stderr(io.StringIO()) as stderr:
            x.save_media([p], directory, frames=2)
            frames.assert_not_called()
            self.assertIn('warning: no mp4 variant for 12345_1', stderr.getvalue())
            self.assertIn('file', p['media'][0])

    def test_syndication(self):
        photo = x.to_tweet(fixture('synd', PHOTO))
        m = photo['media'][0]
        self.assertEqual((m['type'], m['width'], m['height']), ('photo', 1200, 800))
        self.assertEqual(m['alt'], 'example alt text')
        self.assertEqual(photo['likes'], 3)
        self.assertEqual(photo['replies_count'], 0)
        video = x.to_tweet(fixture('synd', VIDEO))['media'][0]
        self.assertIn('1280x720', video['mp4'])
        self.assertIn('640x360', video['mp4_small'])
        self.assertEqual(video['duration_ms'], 12000)
        card_post = x.to_tweet(fixture('synd', CARD))
        self.assertEqual((card_post['likes'], card_post['replies_count']), (10, 1))
        card = card_post['card']
        self.assertEqual(card['domain'], 'github.com')
        self.assertEqual(card['url'], 'https://github.com/example/example-repo')
        poll = x.synd_extra({'card': {'binding_values': {'z': {'string_value': 'x'}, 'a': {'string_value': 'y'}}}})['card']
        self.assertEqual(list(poll['fields']), ['a', 'z'])

    def test_fx(self):
        photo = x.fx_tweet(fixture('fx', PHOTO)['tweet'])
        self.assertEqual((photo['media'][0]['width'], photo['media'][0]['height']), (1200, 800))
        self.assertEqual(photo['media'][0]['alt'], 'example alt text')
        self.assertTrue(photo['media'][0]['url'].endswith('?name=orig'))
        video = x.fx_tweet(fixture('fx', VIDEO)['tweet'])['media'][0]
        self.assertIn('1280x720', video['mp4'])
        self.assertIn('640x360', video['mp4_small'])
        self.assertEqual(video['duration_ms'], 12000)
        card = x.fx_tweet(fixture('fx', CARD)['tweet'])['card']
        self.assertEqual(card['domain'], 'github.com')
        self.assertEqual(card['url'], 'https://github.com/example/example-repo')
        article = x.fx_tweet(fixture('fx', ARTICLE)['tweet'])
        self.assertTrue(article['is_article'])
        self.assertTrue(article['full_text'].startswith('# Example article'))
        self.assertIn('## Section one', article['full_text'])
        self.assertIn('## Section two', article['full_text'])
        self.assertNotIn('Example atomic content', article['full_text'])

    def test_main(self):
        def synd(ident):
            p = FIX / f'synd_{ident}.json'
            return json.loads(p.read_text(encoding='utf-8')) if p.exists() else {'__status': 404}
        def fx(author, ident):
            p = FIX / f'fx_{ident}.json'
            return json.loads(p.read_text(encoding='utf-8'))['tweet'] if p.exists() else None
        with patch.object(x.SYND, 'fetch', side_effect=synd), patch.object(x, 'fetch_page', return_value=''), patch.object(x, 'fetch_fx', side_effect=fx), patch.object(x.time, 'sleep'):
            for fallback in (False, True):
                with patch.object(x.SYND, 'fetch', side_effect=(lambda ident: {'__status': 404}) if fallback else synd), contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(x.main(['--json', PHOTO]), 0)
                    result = json.loads(out.getvalue())
                    self.assertEqual(result['source'], 'fxtwitter' if fallback else 'syndication')
                    post = next(p for p in result['posts'] + result['ancestors'] if p['id'] == PHOTO)
                    self.assertEqual(post['media'][0]['type'], 'photo')
                    if not fallback:
                        self.assertEqual([p['id'] for p in result['posts']], [CARD, PHOTO])
            with patch.object(x.SYND, 'fetch', return_value={'__status': 404}), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(x.main(['--json', CARD]), 0)
                result = json.loads(out.getvalue())
                self.assertEqual(result['source'], 'fxtwitter')
                self.assertEqual([(q['id'], q['quote_of']) for q in result['quoted']], [
                    ('1900000000000000009', CARD),
                    ('1900000000000000010', '1900000000000000009'),
                ])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(x.main(['--json', '--mode', 'post', PHOTO, VIDEO]), 0)
                self.assertEqual(len(json.loads(out.getvalue())), 2)

    def test_save(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(x, 'download_bytes', return_value=b'abc') as download:
            p = x.to_tweet(fixture('synd', PHOTO))
            x.save_media([p], tmp)
            self.assertEqual(Path(p['media'][0]['file']).name, PHOTO + '_1.jpg')
            self.assertTrue(download.call_args.args[0].endswith('?name=large'))
            x.save_media([p], tmp)
            self.assertEqual(download.call_count, 1)
            for url in ['http://pbs.twimg.com/media/a', 'https://evil.example/a', 'https://pbs.twimg.com.evil.example/a']:
                x.save_media([{'id': '123', 'media': [{'type': 'photo', 'url': url}]}], tmp)
            self.assertEqual(download.call_count, 1)
            with self.assertRaises(ValueError):
                x.save_media([{'id': '../evil', 'media': []}], tmp)
            fx = x.fx_tweet(fixture('fx', PHOTO)['tweet'])
            os.unlink(p['media'][0]['file'])
            x.save_media([fx], tmp)
            self.assertTrue(download.call_args.args[0].endswith('?name=large'))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
    def test_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, 'in.mp4')
            out = os.path.join(tmp, 'out.jpg')
            subprocess.run([shutil.which('ffmpeg'), '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=10:duration=6', '-pix_fmt', 'yuv420p', video], check=True)
            self.assertTrue(x.make_frames(video, 6000, 4, out))
            data = Path(out).read_bytes()
            pos = 2
            width = None
            while pos < len(data):
                self.assertEqual(data[pos], 255)
                marker = data[pos + 1]
                length = struct.unpack('>H', data[pos + 2:pos + 4])[0]
                if marker in (0xc0, 0xc1, 0xc2):
                    width = struct.unpack('>H', data[pos + 7:pos + 9])[0]
                    break
                pos += 2 + length
            self.assertGreaterEqual(width, 960)

    def test_encoding(self):
        env = os.environ.copy()
        env.pop('PYTHONUTF8', None)
        env['PYTHONIOENCODING'] = 'cp932'
        p = subprocess.run([sys.executable, str(ROOT / 'x_thread.py'), '\u00b7\u3042'], env=env, capture_output=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn('\u00b7', p.stderr.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
