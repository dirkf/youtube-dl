#!/usr/bin/env python

from __future__ import unicode_literals

# Allow direct execution
import os
import sys
import unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test.helper import (
    assertGreaterEqual,
    expect_warnings,
    get_params,
    gettestcases,
    expect_info_dict,
    try_rm,
    report_warning,
)


import hashlib
import io
import json
import socket
import re

import youtube_dl.YoutubeDL
from youtube_dl.compat import (
    compat_filter as filter,
    compat_map as map,
    compat_http_client,
    compat_urllib_error,
    compat_HTTPError,
)
from youtube_dl.utils import (
    DownloadError,
    ExtractorError,
    format_bytes,
    std_headers,
    UnavailableVideoError,
)
from youtube_dl.extractor import get_info_extractor
from youtube_dl.downloader.common import FileDownloader

RETRIES = 3


class YoutubeDL(youtube_dl.YoutubeDL):
    def __init__(self, *args, **kwargs):
        self.to_stderr = self.to_screen
        self.processed_info_dicts = []
        super(YoutubeDL, self).__init__(*args, **kwargs)

    def report_warning(self, message):
        # Don't accept warnings during tests
        raise ExtractorError(message)

    def process_info(self, info_dict):
        self.processed_info_dicts.append(info_dict)
        return super(YoutubeDL, self).process_info(info_dict)


def _file_md5(fn, length=None):
    with open(fn, 'rb') as f:
        return hashlib.md5(
            f.read(length) if length is not None else f.read()).hexdigest()


defs = gettestcases()


class TestDownload(unittest.TestCase):
    # Parallel testing in nosetests. See
    # http://nose.readthedocs.org/en/latest/doc_tests/test_multiprocess/multiprocess.html
    _multiprocess_shared_ = True

    maxDiff = None

    def __str__(self):
        """Identify each test with the `add_ie` attribute, if available."""

        def strclass(cls):
            """From 2.7's unittest; 2.6 had _strclass so we can't import it."""
            return '%s.%s' % (cls.__module__, cls.__name__)

        add_ie = getattr(self, self._testMethodName).add_ie
        return '%s (%s)%s:' % (self._testMethodName,
                               strclass(self.__class__),
                               ' [%s]' % add_ie if add_ie else '')

    @classmethod
    def addTest(cls, test_method, test_method_name, add_ie):
        test_method.__name__ = str(test_method_name)
        test_method.add_ie = add_ie
        setattr(TestDownload, test_method.__name__, test_method)
        del test_method

    def setUp(self):
        self.defs = defs

# Dynamically generate tests


def generator(test_case, tname):

    def test_template(self):
        ie = youtube_dl.extractor.get_info_extractor(test_case['name'])()
        other_ies = [get_info_extractor(ie_key)() for ie_key in test_case.get('add_ie', [])]
        is_playlist = any(k.startswith('playlist') for k in test_case)
        test_cases = test_case.get(
            'playlist', [] if is_playlist else [test_case])

        def print_skipping(reason):
            print('Skipping %s: %s' % (test_case['name'], reason))
        if not ie.working():
            print_skipping('IE marked as not _WORKING')
            return

        for tc in test_cases:
            info_dict = tc.get('info_dict', {})
            if not (info_dict.get('id') and info_dict.get('ext')):
                raise Exception('Test definition incorrect. The output file cannot be known. Are both \'id\' and \'ext\' keys present?')

        if 'skip' in test_case:
            print_skipping(test_case['skip'])
            return
        for other_ie in other_ies:
            if not other_ie.working():
                print_skipping('test depends on %sIE, marked as not WORKING' % other_ie.ie_key())
                return

        params = get_params(test_case.get('params', {}))
        params['outtmpl'] = tname + '_' + params['outtmpl']
        if is_playlist and 'playlist' not in test_case:
            params.setdefault('extract_flat', 'in_playlist')
            params.setdefault('skip_download', True)

        if 'user_agent' in params:
            std_headers['User-Agent'] = params['user_agent']

        if 'referer' in params:
            std_headers['Referer'] = params['referer']

        for h in params.get('headers', []):
            h = h.split(':', 1)
            if len(h) > 1:
                std_headers[h[0]] = h[1]

        ydl = YoutubeDL(params, auto_init=False)
        ydl.add_default_info_extractors()
        finished_hook_called = set()

        def _hook(status):
            if status['status'] == 'finished':
                finished_hook_called.add(status['filename'])
        ydl.add_progress_hook(_hook)
        expect_warnings(ydl, test_case.get('expected_warnings', []))

        def get_tc_filename(tc):
            return ydl.prepare_filename(tc.get('info_dict', {}))

        res_dict = None

        def try_rm_tcs_files(tcs=None):
            if tcs is None:
                tcs = test_cases
            for tc in tcs:
                tc_filename = get_tc_filename(tc)
                try_rm(tc_filename)
                try_rm(tc_filename + '.part')
                try_rm(os.path.splitext(tc_filename)[0] + '.info.json')
        try_rm_tcs_files()
        try:
            try_num = 1
            while True:
                try:
                    # We're not using .download here since that is just a shim
                    # for outside error handling, and returns the exit code
                    # instead of the result dict.
                    res_dict = ydl.extract_info(
                        test_case['url'],
                        force_generic_extractor=params.get('force_generic_extractor', False))
                except (DownloadError, ExtractorError) as err:
                    # Check if the exception is not a network related one
                    if not err.exc_info[0] in (compat_urllib_error.URLError, socket.timeout, UnavailableVideoError, compat_http_client.BadStatusLine) or (err.exc_info[0] == compat_HTTPError and err.exc_info[1].code == 503):
                        raise

                    if try_num == RETRIES:
                        report_warning('%s failed due to network errors, skipping...' % tname)
                        return

                    print('Retrying: {0} failed tries\n\n##########\n\n'.format(try_num))

                    try_num += 1
                else:
                    break

            if is_playlist:
                self.assertTrue(res_dict['_type'] in ['playlist', 'multi_video'])
                self.assertTrue('entries' in res_dict)
                expect_info_dict(self, res_dict, test_case.get('info_dict', {}))

            if 'playlist_mincount' in test_case:
                assertGreaterEqual(
                    self,
                    len(res_dict['entries']),
                    test_case['playlist_mincount'],
                    'Expected at least %d in playlist %s, but got only %d' % (
                        test_case['playlist_mincount'], test_case['url'],
                        len(res_dict['entries'])))
            if 'playlist_count' in test_case:
                self.assertEqual(
                    len(res_dict['entries']),
                    test_case['playlist_count'],
                    'Expected %d entries in playlist %s, but got %d.' % (
                        test_case['playlist_count'],
                        test_case['url'],
                        len(res_dict['entries']),
                    ))
            if 'playlist_duration_sum' in test_case:
                got_duration = sum(e['duration'] for e in res_dict['entries'])
                self.assertEqual(
                    test_case['playlist_duration_sum'], got_duration)

            # Generalize both playlists and single videos to unified format for
            # simplicity
            if 'entries' not in res_dict:
                res_dict['entries'] = [res_dict]

            for tc_num, tc in enumerate(test_cases):
                tc_res_dict = res_dict['entries'][tc_num]
                # First, check test cases' data against extracted data alone
                expect_info_dict(self, tc_res_dict, tc.get('info_dict', {}))
                # Now, check downloaded file consistency
                tc_filename = get_tc_filename(tc)
                if not test_case.get('params', {}).get('skip_download', False):
                    self.assertTrue(os.path.exists(tc_filename), msg='Missing file ' + tc_filename)
                    self.assertTrue(tc_filename in finished_hook_called)
                    expected_minsize = tc.get('file_minsize', 10000)
                    if expected_minsize is not None:
                        if params.get('test'):
                            expected_minsize = max(expected_minsize, 10000)
                        got_fsize = os.path.getsize(tc_filename)
                        assertGreaterEqual(
                            self, got_fsize, expected_minsize,
                            'Expected %s to be at least %s, but it\'s only %s ' %
                            (tc_filename, format_bytes(expected_minsize),
                                format_bytes(got_fsize)))
                    if 'md5' in tc:
                        md5_for_file = _file_md5(tc_filename) if not params.get('test') else _file_md5(tc_filename, FileDownloader._TEST_FILE_SIZE)
                        self.assertEqual(tc['md5'], md5_for_file)
                # Finally, check test cases' data again but this time against
                # extracted data from info JSON file written during processing
                info_json_fn = os.path.splitext(tc_filename)[0] + '.info.json'
                self.assertTrue(
                    os.path.exists(info_json_fn),
                    'Missing info file %s' % info_json_fn)
                with io.open(info_json_fn, encoding='utf-8') as infof:
                    info_dict = json.load(infof)
                expect_info_dict(self, info_dict, tc.get('info_dict', {}))
        finally:
            try_rm_tcs_files()
            if is_playlist and res_dict is not None and res_dict.get('entries'):
                # Remove all other files that may have been extracted if the
                # extractor returns full results even with extract_flat
                res_tcs = [{'info_dict': e} for e in res_dict['entries']]
                try_rm_tcs_files(res_tcs)

    return test_template


# And add them to TestDownload
for n, test_case in enumerate(defs):
    tname = 'test_' + str(test_case['name'])
    i = 1
    while hasattr(TestDownload, tname):
        tname = 'test_%s_%d' % (test_case['name'], i)
        i += 1
    test_method = generator(test_case, tname)
    ie_list = ','.join(test_case.get('add_ie', []))
    TestDownload.addTest(test_method, tname, ie_list)


def tests_for_ie(ie_key):
    return filter(
        lambda a: callable(getattr(TestDownload, a, None)),
        filter(lambda a: re.match(r'test_%s(?:_\d+)?$' % ie_key, a),
               dir(TestDownload)))


def gen_test_suite(ie_key):
    def test_all(self):
        print(self)
        suite = unittest.TestSuite(
            map(TestDownload, tests_for_ie(ie_key)))
        result = self.defaultTestResult()
        suite.run(result)
        print('Errors: %d\t Failures: %d\tSkipped: %d' %
              tuple(map(len, (result.errors, result.failures, result.skipped))))
        print('Expected failures: %d\tUnexpected successes: %d' %
              tuple(map(len, (result.expectedFailures, result.unexpectedSuccesses))))
        return result

    return test_all


for ie_key in set(
    map(lambda a: a[5:],
        filter(
            lambda x: callable(getattr(TestDownload, x, None)),
            filter(
                lambda t: re.match(r"test_.+(?<!(?:_all|.._\d|._\d\d|_\d\d\d))$", t),
                dir(TestDownload))))):
    test_all = gen_test_suite(ie_key)
    TestDownload.addTest(test_all, 'test_%s_all' % ie_key, 'Test all: %s' % ie_key)

if __name__ == '__main__':
    unittest.main()
