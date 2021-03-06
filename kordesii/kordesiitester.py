"""
Test case support for DC3-Kordesii. Decoder output is stored in a json file per decoder. To run test cases,
decoder is re-run and compared to previous results.
"""
from __future__ import print_function

import glob
import json
import multiprocessing as mp
# Standard imports
import os
from timeit import default_timer

import kordesii.kordesiireporter


FIELD_LAST_UPDATE = "last_update"

DEFAULT_EXCLUDE_FIELDS = [
    kordesii.kordesiireporter.FIELD_DEBUG,
    kordesii.kordesiireporter.FIELD_IDB,
    FIELD_LAST_UPDATE
]

DEFAULT_INCLUDE_FIELDS = [
    kordesii.kordesiireporter.FIELD_STRINGS,
    kordesii.kordesiireporter.FIELD_FILES
]


def multiproc_test_wrapper(args):
    """Wrapper function for running tests in multiple processes."""
    tester_instance = args[0]
    try:
        return tester_instance.async_test(*args[1:])
    except KeyboardInterrupt:
        return


class kordesiitester(object):
    """
    DC3-Kordesii test case class
    """
    # Constants
    INPUT_FILE_PATH = "input_file"
    FILE_EXTENSION = ".json"
    DECODER = "decoder"
    RESULTS = "results"
    PASSED = "passed"
    ERRORS = "errors"
    DEBUG = "debug"
    IDA_LOG = "ida_log"
    RUN_TIME = "run_time"

    # Properties
    reporter = None
    results_dir = None

    def __init__(self, reporter, results_dir):
        self.reporter = reporter
        self.results_dir = results_dir

    def gen_results(self, decoder_name, input_file_path):
        """
        Generate JSON results for the given file using the given decoder name.
        """

        data = open(input_file_path, "rb").read()
        self.reporter.run_decoder(decoder_name, data=data)
        self.reporter.metadata[self.INPUT_FILE_PATH] = input_file_path

    def list_test_files(self, decoder_name):
        """
        Generate list of files (test cases) for decoder
        """

        filelist = []
        for metadata in self.parse_results_file(self.get_results_filepath(decoder_name)):
            filelist.append(metadata[self.INPUT_FILE_PATH])
        return filelist

    def get_results_filepath(self, decoder_name):
        """
        Get a results file path based on the decoder name provided and the
        previously specified output directory.
        """

        file_name = decoder_name + self.FILE_EXTENSION
        file_path = os.path.join(self.results_dir, file_name)

        return file_path

    def parse_results_file(self, results_file_path):
        """
        Parse the the JSON results file and return the parsed data.
        """

        with open(results_file_path) as results_file:
            data = json.load(results_file)

        # The results file data is expected to be a list of metadata dictionaries
        assert type(data) == list and all(type(a) is dict for a in data)

        return data

    def update_test_results(self,
                            results_file_path,
                            results_data,
                            replace=True):
        """
        Update results in the results file with the passed in results data. If the
        file path for the results data matches a file path that is already found in
        the passed in results file, then the replace argument comes into play to
        determine if the record should be replaced.
        """

        # The results data is expected to be a dictionary representing results for a single file
        assert type(results_data) is dict

        results_file_data = []
        if os.path.isfile(results_file_path):
            results_file_data = self.parse_results_file(results_file_path)

            # Check if there is a duplicate file path already in the results path
            index = 0
            found = False
            while index < len(results_file_data) and not found:
                metadata = results_file_data[index]
                if metadata[self.INPUT_FILE_PATH] == results_data[self.INPUT_FILE_PATH]:
                    if replace:
                        results_file_data[index] = results_data
                    found = True
                index += 1

            # If no duplicate found, then append the passed in results data to existing results
            if not found:
                results_file_data.append(results_data)

        else:
            # Results file should be a list of metadata dictionaries
            results_file_data.append(results_data)

        # Write updated data to results file
        pretty_data = self.reporter.pprint(results_file_data)
        with open(results_file_path, "w") as results_file:
            results_file.write(pretty_data)

    def remove_test_results(self, decoder_name, filenames):
        """
        remove filenames from test cases for decoder_name

        return files that were removed
        """
        removed_files = []
        results_file_data = []
        for metadata in self.parse_results_file(self.get_results_filepath(decoder_name)):
            if metadata[self.INPUT_FILE_PATH] in filenames:
                removed_files.append(metadata[self.INPUT_FILE_PATH])
            else:
                results_file_data.append(metadata)

        pretty_data = self.reporter.pprint(results_file_data)
        with open(self.get_results_filepath(decoder_name), "w") as results_file:
            results_file.write(pretty_data)

        return removed_files

    def run_tests(
            self,
            decoder_names=None,
            field_names=None,
            ignore_field_names=DEFAULT_EXCLUDE_FIELDS,
            nprocs=None):
        """
        Run tests and compare produced results to expected results.

        Arguments:
            decoder_names (list):
                A list of decoder names to run tests for. If the list is empty (default),
                then test cases for all decoders will be run.
            field_names(list):
                A restricted list of fields (metadata key values) that should be compared
                during testing. If the list is empty (default), then all fields, except those in
                ignore_field_names will be compared.

        :yields: tuple containing (passed boolean, dictionary containing test_results)
        """

        if field_names is None:
            field_names = []

        results_file_list = glob.glob(os.path.join(self.results_dir, "*{0}".format(self.FILE_EXTENSION)))
        if not decoder_names:
            decoder_names = []
        if not field_names:
            field_names = []

        # Determine files to test (this will be a list of JSON files)
        test_case_file_paths = []
        if len(decoder_names) > 0:
            for decoder_name in decoder_names:
                results_file_path = self.get_results_filepath(decoder_name)

                if results_file_path in results_file_list:
                    test_case_file_paths.append(results_file_path)
                else:
                    print("Results file not found for {0} decoder".format(decoder_name))
                    print("File not found = {0}".format(results_file_path))
        else:
            test_case_file_paths = results_file_list

        cores = mp.cpu_count()
        procs = nprocs or (3 * cores) // 4
        pool = mp.Pool(processes=procs)

        tests = []
        # Just for nicer formatting...
        decoder_len = 0
        filename_len = 0
        # Parse test case/results files, run tests, and compare expected results to produced results
        for results_file_path in test_case_file_paths:
            results_data = self.parse_results_file(results_file_path)
            decoder_name = os.path.splitext(os.path.basename(results_file_path))[0]

            for result_data in results_data:
                decoder_len = max(decoder_len, len(decoder_name))
                filename_len = max(filename_len, len(os.path.basename(result_data[self.INPUT_FILE_PATH])))
                tests.append((self, result_data, decoder_name, field_names, ignore_field_names))

        finished_tests = 0
        digits = len(str(len(tests)))

        # While the tests will start in the order they were added, they will be yielded roughly in the
        # order they complete.
        test_iter = pool.imap_unordered(multiproc_test_wrapper, tests)
        pool.close()
        try:
            for results in test_iter:
                # Add an info dict to the returned results
                # Built with formatting here since we have knowledge of all test cases
                finished_tests += 1
                test_info = {
                    'finished': str(finished_tests).zfill(digits),
                    'total': str(len(tests)).zfill(digits),
                    'decoder': results[1][self.DECODER].ljust(decoder_len),
                    'filename': os.path.basename(results[1][self.INPUT_FILE_PATH]).ljust(filename_len),
                    'run_time': results[1][self.RUN_TIME]
                }
                yield results + (test_info,)
        except KeyboardInterrupt:
            pool.terminate()
            raise

    def async_test(self, result_data, decoder_name, field_names, ignore_field_names):
        """Test running logic, separated into its own function for multi-processing purposes."""
        start_time = default_timer()
        input_file_path = result_data[self.INPUT_FILE_PATH]
        self.gen_results(decoder_name, input_file_path)
        new_results = self.reporter.metadata
        passed, test_results = self.compare_results(
            result_data,
            new_results,
            field_names,
            ignore_field_names=ignore_field_names
        )
        if len(self.reporter.errors) > 0:
            passed = False

        done_time = default_timer()
        run_time = done_time - start_time

        # NOTE: Do not use a namedtuple, as they are not pickle-able.
        test_result = {
            self.DECODER: decoder_name,
            self.INPUT_FILE_PATH: input_file_path,
            self.PASSED: passed,
            self.ERRORS: list(self.reporter.errors),
            self.DEBUG: self.reporter.get_debug(),
            self.IDA_LOG: self.reporter.ida_log,
            self.RESULTS: test_results,
            self.RUN_TIME: run_time
        }

        return passed, test_result

    def compare_results(self,
                        results_a,
                        results_b,
                        field_names=[],
                        ignore_field_names=DEFAULT_EXCLUDE_FIELDS):
        """
        Compare two result sets. Only the fields (metadata key values) in
        the list will be compared.
        """

        passed = True
        test_results = {}

        # Cursory check to remove FILE_INPUT_PATH key from results since it is a custom added field for test cases
        if self.INPUT_FILE_PATH in results_a:
            results_a = dict(results_a)
            del results_a[self.INPUT_FILE_PATH]
        if self.INPUT_FILE_PATH in results_b:
            results_b = dict(results_b)
            del results_b[self.INPUT_FILE_PATH]

        # Begin comparing results
        if len(field_names) > 0:
            for field_name in field_names:
                comparer = self.compare_results_field(results_a, results_b, field_name)
                test_results[field_name] = comparer
                if not comparer.passed():
                    passed = False

        else:
            for ignore_field in ignore_field_names:
                results_a.pop(ignore_field, None)
                results_b.pop(ignore_field, None)
            if set(results_a.keys()) != set(results_b.keys()):
                passed = False
                all_keys = set(results_a.keys()).union(results_b.keys())
                for key in all_keys:
                    test_results[key] = self.compare_results_field(results_a, results_b, key)
            else:
                for key in results_a:
                    comparer = self.compare_results_field(results_a, results_b, key)
                    test_results[key] = comparer
                    if not comparer.passed():
                        passed = False

        return passed, test_results

    def compare_results_field(self, results_a, results_b, field_name):
        """
        Compare the values for a single results field in the two passed in results.
        """

        assert type(results_a) is dict and type(results_b) is dict
        comparer = ResultComparison(field_name)

        # Confirm key is found in at least one of the passed in result sets
        if field_name not in results_a and field_name not in results_b:
            return comparer

        # Establish comparer object to compare results with
        if field_name == kordesii.kordesiireporter.FIELD_STRINGS:
            comparer = StringsComparison()
        elif field_name == kordesii.kordesiireporter.FIELD_FILES:
            comparer = FilesComparison()

        # Compare results and return result
        if field_name in results_a:
            result_a = results_a[field_name]
        else:
            result_a = []
        if field_name in results_b:
            result_b = results_b[field_name]
        else:
            result_b = []

            # Compare results and return result
        comparer.compare(result_a, result_b)
        return comparer

    def format_test_result(self, test_result, json_format=False):
        """
        Formats test result based on provided parameters.
        Expects single test result format roduced by run_tests() function.

        :param dict test_result: Dictionary containing a single test_result.
        :param bool json_format: Whether to format results in json or user-friendly text.

        :return str: A string containing the formatted results.
        """

        if json_format:
            filtered_result = {
                self.DECODER: test_result[self.DECODER],
                self.INPUT_FILE_PATH: test_result[self.INPUT_FILE_PATH],
                self.PASSED: test_result[self.PASSED]
            }
            # Add logs if failed.
            if not test_result[self.PASSED]:
                filtered_result.update({
                    self.ERRORS: test_result[self.ERRORS],
                    self.DEBUG: test_result[self.DEBUG],
                    self.RESULTS: test_result[self.RESULTS]
                })
                if test_result[self.ERRORS]:
                    filtered_result[self.IDA_LOG] = test_result[self.IDA_LOG]

            return json.dumps(filtered_result, indent=4)

        else:
            filtered_output = u""
            filtered_output += u"{0}: {1}\n".format(self.DECODER, test_result[self.DECODER])
            filtered_output += u"{0}: {1}\n".format(self.INPUT_FILE_PATH, test_result[self.INPUT_FILE_PATH])
            filtered_output += u"{0}: {1}\n".format(self.PASSED, test_result[self.PASSED])
            # Add logs if failed.
            if not test_result[self.PASSED]:
                filtered_output += u"{0}: {1}".format(self.ERRORS, "\n" if test_result[self.ERRORS] else "None\n")
                if test_result[self.ERRORS]:
                    for entry in test_result[self.ERRORS]:
                        filtered_output += u"\t{0}\n".format(entry)
                filtered_output += u"{0}: {1}".format(self.DEBUG, "\n" if test_result[self.DEBUG] else "None\n")
                if test_result[self.DEBUG]:
                    for entry in test_result[self.DEBUG]:
                        filtered_output += u"\t{0}\n".format(entry)
                filtered_output += u"{0}:\n".format(self.RESULTS)
                for key in test_result[self.RESULTS]:
                    filtered_output += u"{0}\n".format(
                        str(test_result[self.RESULTS][key]).decode('utf8', 'replace'))
                if test_result[self.ERRORS]:
                    filtered_output += u"{0}:\n{1}\n".format(self.IDA_LOG, test_result[self.IDA_LOG])
            filtered_output += u"\n"

            return filtered_output


class ResultComparison(object):
    # Test Statuses
    PASS = 'Pass'
    FAIL = 'Fail'
    SUPERSET = 'Fail - Superset'

    def __init__(self, field):
        self.field = field
        self.code = ResultComparison.FAIL
        self.missing = []  # Entries found in test case but not new results
        self.unexpected = []  # Entries found in new results but not test case

    def compare(self, test_case_results, new_results):
        """Compare two result sets and document any differences."""

        for item in test_case_results:
            if len(item) > 0 and item not in new_results:
                self.missing.append(item)

        for item in new_results:
            if len(item) > 0 and item not in test_case_results:
                self.unexpected.append(item)

        if self.missing:
            self.code = ResultComparison.FAIL
        elif self.unexpected:
            self.code = ResultComparison.SUPERSET
        else:
            self.code = ResultComparison.PASS

    def passed(self):
        """Return if the comparison passed based on the code value."""

        if self.code in (ResultComparison.FAIL, ResultComparison.SUPERSET):
            return False
        else:
            return True

    def get_report(self, json=False, tabs=1):
        """
        If json parameter is False, get report as a string.
        If json parameter is True, get report as a dictionary.
        """

        if json:
            return self.__dict__
        else:
            tab = tabs * "\t"
            tab_1 = tab + "\t"
            tab_2 = tab_1 + "\t"
            report = tab + "{}:\n".format(self.field)
            report += tab_1 + "Code: {}\n".format(self.code)
            if self.missing:
                report += tab_1 + "Missing:\n"
                for item in self.missing:
                    report += tab_2 + "{!r}\n".format(item)
            if self.unexpected:
                report += tab_1 + "Unexpected:\n"
                for item in self.unexpected:
                    report += tab_2 + "{!r}\n".format(item)

            return report.rstrip()

    def __str__(self):
        return self.get_report()

    def __repr__(self):
        return self.__str__()


class StringsComparison(ResultComparison):
    def __init__(self):
        super(StringsComparison, self).__init__(kordesii.kordesiireporter.FIELD_STRINGS)


class FilesComparison(ResultComparison):
    def __init__(self):
        super(FilesComparison, self).__init__(kordesii.kordesiireporter.FIELD_FILES)


class MyEncoder(json.JSONEncoder):
    def default(self, o):
        return o.__dict__
