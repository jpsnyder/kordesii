
import abc
import copy
import hashlib
import functools
import itertools
import os
import re
import yara
import sys
import warnings

import idaapi
import idautils
import idc

from kordesii.kordesiiidahelper import append_debug, append_string
from kordesii.utils import function_tracingutils
from kordesii.utils.functioncreator import create_function_precise

INVALID = -1
UNUSED = None
_YARA_MATCHES = []

# Codecs used to detect encoding of strings.
CODE_PAGES = [
    'ascii',
    'utf-32-be', 'utf-32-le', 'utf-16-be', 'utf-16-le', 'utf-8',  # General (utf-7 omitted)
    'gb18030', 'gbk',  # Unified Chinese
    'gb2312', 'hz',  # Simplified Chinese
    'big5hkscs', 'big5',  # Traditional Chinese (cp950 omitted)
    'koi8-r', 'iso8859-5', 'cp1251', 'mac-cyrillic',  # Cyrillic (cp866, cp855 omitted)
    'cp949',  # Korean (johab, iso2022-kr omitted)
    'iso8859-6', 'cp1256',  # Arabic (cp864, cp720 omitted)
    'latin1',  # If all else fails, latin1 is always is successful.
]
INPUT_FILE_PATH = idc.GetInputFilePath()
# Put these here for increased robustness. Please don't depend on these very often.
ENCODED_STRINGS = []
DECODED_STRINGS = []

# CODEC to use for displaying strings in IDA, etc.
DISPLAY_CODE = 'cp437' if sys.platform == 'win32' else 'ascii'


class SuperFunc_t(object):
    """
    Description:
        Effectively extends func_t to also know its name and all its non-recursive xrefs and knows
        how to rename itself.

    Fields:
        function_obj - The idaapi.func_t object for this function. (<ea> must be within a function.)
        name - The name of the function.
        xrefs_to - EA's for all of the non-recursive references to this function's startEA.
        xref_count - len(xrefs_to)

    Input:
        ea - An EA within the function.
        identifier - The id of the YARA rule that hit in this function
        create_if_not_exists - If true, uses IN_Dev_Repo's function creator to create a function containing <ea>
    """

    def __init__(self, ea, identifier=UNUSED, create_if_not_exists=True):
        super(SuperFunc_t, self).__init__()
        self.origin_ea = ea
        self.identifier = identifier
        self.function_obj = idaapi.get_func(ea)
        if not self.function_obj:
            if create_if_not_exists:
                if create_function_precise(ea, False):
                    self.function_obj = idaapi.get_func(ea)
                    append_debug("Created function at 0x%X" % self.function_obj.startEA)
                else:
                    raise AttributeError("No function at 0x%X" % ea)
            else:
                raise AttributeError("No function at 0x%X" % ea)
        if self.function_obj:
            self.startEA = self.function_obj.startEA
            self.endEA = self.function_obj.endEA
        self.name = idaapi.get_func_name(self.function_obj.startEA)
        self.xrefs_to = [ref.frm for ref in idautils.XrefsTo(self.function_obj.startEA)
                         if idaapi.get_func_name(ref.frm) != self.name]
        self.xref_count = len(self.xrefs_to)
        self._flowchart = None

    def __eq__(self, other):
        return self.__hash__() == other.__hash__()

    def __hash__(self):
        return self.__repr__().__hash__()

    def __str__(self):
        return '%s 0x%X - 0x%X' % (self.name, self.function_obj.startEA, self.function_obj.endEA)

    def __repr__(self):
        return '<SuperFunc_t : {}() : {:#08x} - {:#08x}>'.format(self.name, self.startEA, self.endEA)

    def heads(self, start=None, reverse=False):
        """
        Iterates all the heads for the given function.

        :param start: Start address (defaults to startEA or endEA)
        :param reverse:  Direction to iterate
        :return:
        """
        if not self._flowchart:
            self._flowchart = function_tracingutils.FlowChart(self.startEA)
        if not start:
            start = self.endEA if reverse else self.startEA

        for ea in self._flowchart.bfs_iter_heads(start, reverse=reverse):
            yield ea

    def rename(self, new_name):
        """
        Attempts to apply new_name to the object at <ea>. If more than one object starts at <ea>, the
        largest object will be renamed. If that name already exists, let IDA resolve the collission
        and then return that name. If new_name is "", reset the name to IDA's default.

        :param str new_name: The desired new name for the function.

        :return str: The name that ended up getting set (unless no name was set, then return None).
        """
        if new_name == '':
            if idaapi.set_name(self.function_obj.startEA, new_name):
                return idaapi.get_name(self.function_obj.startEA)
            else:
                append_debug('Failed to reset name at 0x%X' % self.function_obj.startEA)
        elif idaapi.do_name_anyway(self.function_obj.startEA, new_name):
            self.name = idaapi.get_name(self.function_obj.startEA)
            if self.name != new_name:
                append_debug('IDA changed name "%s" to "%s"' % (new_name, self.name))
            return self.name
        else:
            append_debug('Failed to rename at 0x%X' % self.function_obj.startEA)


@functools.total_ordering
class EncodedString(object):
    """
    Description:
        Object to hold data about an encoded string. Effectively extends StringItem, but
        avoids all the icky IDA innards.

        If a field is not used, it is recommended to set it to UNUSED in your constructor.

        It is recommended to inherit this class rather than alter it in most cases.

    Fields:
        string_location - The EA at which the encoded_data starts.
        string_reference - The EA from which the string is referenced. Defaults to INVALID.
        size - The size of the encoded_data. When set in the constructor, causes encoded_data to be
               populated. Defaults to INVALID.
        offset - The offset from string_location at which the actual encoded_data starts. Often unused.
                 Defaults to INVALID.
        key - The key used for decoding. Generally, this os only set when the key differs by string.
        encoded_data - The raw data from the file that we intend to decode. Is populated by get_bytes
                      if size != INVALID. Defaults to INVALID.
        decoded_data - The string's value after it has been decoded. Defaults to INVALID.
        code_page - Code page used to decode string when unicode() is called.

    Input:
        string_location - Required. Wait until you know this to build the object if at all possible.
        string_reference - The location the string is referenced from. Often helpful
        size - The size of the string. Required to use self.get_bytes.
        offset - Used when there is an offset based accessing scheme.
        key - Used when there is a key that can vary by string.
    """

    _MAX_COMMENT_LENGTH = 130
    _MAX_NAME_LENGTH = 30

    # TODO: Is this "INVALID", "UNUSED" stuff necessary? Can we just have them as None?
    def __init__(self, string_location,
                 string_reference=INVALID, size=INVALID, offset=INVALID, key=INVALID):
        super(EncodedString, self).__init__()
        self.string_location = string_location
        self.string_reference = string_reference
        self.size = size
        self.offset = offset
        self.key = key
        self.encoded_data = self.get_bytes() if size not in (INVALID, UNUSED) else INVALID
        self.decoded_data = INVALID
        self.code_page = None
        self._xrefs_to = None

    @property
    def decoded_string(self):
        warnings.warn(
            'decoded_string attribute is deprecated, please use decoded_data', DeprecationWarning)
        return self.decoded_data

    @decoded_string.setter
    def decoded_string(self, value):
        warnings.warn(
            'decoded_string attribute is deprecated, please use decoded_data', DeprecationWarning)
        self.decoded_data = value

    @classmethod
    def factory(cls, string_location, string_reference, size=INVALID, offset=INVALID, key=INVALID):
        """
        Factory function to generate an EncodedString or EncodedStackString based on type.

        :param string_location:
            Data segment pointer for static strings or stack pointer for stack strings.
        :param string_reference:
            The location the string is referenced from.
            This is required to pull the stack frame when string_location is a stack pointer.
        :param size: The size of the string. Required to use self.get_bytes.
        :param offset: Used when there is an offset based accessing scheme.
        :param key: Used when there is a key that can vary by string.
        """
        if idc.isLoaded(string_location):
            return EncodedString(
                string_location, string_reference, size=size, offset=offset, key=key)

        # otherwise assume string_location is a pointer within the stack
        # (using function_tracingutils's CPU emulator) and create an EncodedStackString object.
        stack = idc.GetFrame(string_reference)
        # FIXME: This method isn't always super accurate because... IDA
        stack_offset = (
                string_location
                + function_tracingutils.RSP_OFFSET
                + idc.GetFrameLvarSize(string_reference)
                - function_tracingutils.STACK_BASE
        )

        return EncodedStackString(
            stack, stack_offset, string_reference=string_reference,
            size=size, offset=offset, key=key)

    def _compare_key(self):
        # Sort by where it was found then where it is referenced
        return self.string_location, self.string_reference, self.decoded_data

    def __hash__(self):
        return hash(self._compare_key())

    def __eq__(self, other):
        return self._compare_key() == other._compare_key()

    def __lt__(self, other):
        return self._compare_key() < other._compare_key()

    def report(self):
        """
        Generates a text report of the EncodedString object.

        :return str: Unicode string containing text report.
        """
        # We have to return repr as a unicode because of the decoded string.
        text = u''
        if self.string_location not in (INVALID, UNUSED):
            text += u'EA:  0x%X\n' % self.string_location
        if self.string_reference not in (INVALID, UNUSED):
            text += u'Ref: 0x%X\n' % self.string_reference
        if self.offset not in (INVALID, UNUSED):
            text += u'Offset: 0x%X\n' % self.offset
        if self.decoded_data not in (INVALID, UNUSED):
            dec_string = unicode(self)
            if self.code_page:
                text += u'Detected Code Page: {}\n'.format(self.code_page)
            text += u'Raw Dec: {!r}\n'.format(self.decoded_data)
            text += u'Dec: {}'.format(dec_string)
        return text

    def __str__(self):
        return unicode(self).encode('unicode-escape')

    def __unicode__(self):
        return self.decode_unknown_charset()

    @property
    def xrefs_to(self):
        if self._xrefs_to is None:
            self._xrefs_to = [ref.frm for ref in idautils.XrefsTo(self.string_location)]
        return self._xrefs_to

    @property
    def display_name(self):
        """Returns an IDA friendly, printable name for the decoded string."""
        return unicode(self).encode(DISPLAY_CODE, 'replace')

    def rename(self, name=None):
        """
        Renames (and comments) the string variable in IDA.

        :param str name: New name to given encoded string. (defaults to decoded_string)
        """
        name = name or self.display_name
        if not name:
            append_debug(
                'Unable to rename encoded string due to no decoded string: {!r}'.format(self),
                log_token='[!]')
            return

        # Add comment
        comment = '"{}"'.format(name[:self._MAX_COMMENT_LENGTH])
        if len(name) > self._MAX_COMMENT_LENGTH:
            comment += ' (truncated)'
        if self.string_location not in (INVALID, UNUSED):
            idc.MakeRptCmt(self.string_location, comment)
        if self.string_reference not in (INVALID, UNUSED):
            idc.MakeRptCmt(self.string_reference, comment)

        # Set variable name
        if self.string_location not in (INVALID, UNUSED):
            idaapi.do_name_anyway(self.string_location, name[:self._MAX_NAME_LENGTH])

    def patch(self, fill_char=None, define=True):
        """
        Patches the original encoded string with the decoded string.

        :param str fill_char:
            Character to use to fill left over space if decoded data
            is shorter than its encoded data. (defaults to leaving the original data)
        :param bool define: Whether to define the string after patching.
        """
        if self.decoded_data in (INVALID, UNUSED):
            return
        if self.string_location in (INVALID, UNUSED, idc.BADADDR):
            return
        decoded_data = self.as_bytes
        if fill_char:
            decoded_data += fill_char * (len(self.encoded_data) - len(decoded_data))
        try:
            idaapi.patch_many_bytes(self.startEA, decoded_data)
            if define:
                self.define()
        except TypeError:
            append_debug("String type for decoded string from location 0x{:08x}.".format(self.startEA))

    # TODO: Can this just be embedded in patch()?
    def define(self):
        """
        Defines the string in the IDB.
        """
        try:
            idc.MakeUnknown(self.startEA, self.byte_length, idc.DOUNK_SIMPLE)
            idaapi.make_ascii_string(self.startEA, self.byte_length, self.string_type)
        except:
            append_debug('Unable to define string at 0x%X' % self.startEA)

    @property
    def startEA(self):
        if self.string_location in (UNUSED, INVALID, idc.BADADDR):
            return INVALID
        else:
            return self.string_location + (self.offset if self.offset not in [INVALID, UNUSED] else 0)

    @property
    def endEA(self):
        start_ea = self.startEA
        length = self.byte_length
        if start_ea == INVALID or length == INVALID:
            return INVALID
        else:
            return start_ea + length

    # TODO: This property probably can be replace by __str__()
    @property
    def as_bytes(self):
        # patch_many_bytes wants an ascii string (str) of the bytes (or their char form)
        # therefore for wide chars, this conversion is necessary.
        # This also allows us to calculate the on-disk size.
        if not self.decoded_data:
            return INVALID
        else:
            return ''.join(itertools.imap(str, self.decoded_data))

    # TODO: This attribute is not necessary.
    @property
    def byte_length(self):
        if not self.decoded_data:
            return INVALID
        else:
            return len(self.as_bytes)

    @property
    def string_type(self):
        if not self.decoded_data:
            return INVALID
        else:
            return idc.ASCSTR_UNICODE if isinstance(self.decoded_data, unicode) else idc.ASCSTR_C

    def calc_size(self, width=1):
        """
        Description:
            Search for the next null to end the string and update.

        Output:
            Returns size if it is found, idc.BADADDR otherwise.
            Updates encoded_data if self.size was INVALID.
        """
        end_location = idc.FindBinary(self.string_location, idc.SEARCH_DOWN, "00 " * width)
        if end_location == idc.BADADDR:
            return idc.BADADDR

        self.size = end_location - self.string_location
        while self.size % width:  # ensure unicode strings are a valid length
            self.size += 1

        if self.encoded_data == INVALID:
            self.encoded_data = self.get_bytes()

        return self.size

    # TODO: Perhaps this should be part of a property for the "encoded_data" attribute?
    def get_bytes(self):
        """
        Description:
            Get self.size bytes at self.string_location.

        Input:
            Requires a valid self.size.

        Output:
            Returns self.size bytes at self.string_location

        Throws:
            ValueError - Size was not valid.
        """
        if self.size == INVALID:
            raise ValueError("Size was never calculated!")
        bytes = idaapi.get_many_bytes(self.string_location, self.size) if self.size else ''
        return bytes if bytes is not None else INVALID

    def _num_raw_bytes(self, string):
        """
        Returns the number of raw bytes found in the given unicode string
        """
        count = 0
        for char in string:
            char = char.encode('unicode-escape')
            count += char.startswith(b'\\x') + char.startswith(b'\\u') * 2
        return count

    def decode_unknown_charset(self):
        """
        Returns a decoded string using the best guess codec.
        """
        if self.decoded_data in (INVALID, UNUSED):
            return u''

        # First see if the decoder already gave us unicode.
        if isinstance(self.decoded_data, unicode):
            return self.decoded_data

        # If a code page was set (either by us or the decoder) use that.
        # If the code page doesn't work... move on.
        if self.code_page:
            try:
                return self.decoded_data.decode(self.code_page)
            except UnicodeDecodeError:
                pass

        # TODO: Use chardet if they ever support utf16 without BOM.
        best_score = len(self.decoded_data)
        best_code_page = None
        best_output = None
        for code_page in CODE_PAGES:
            try:
                output = self.decoded_data.decode(code_page).rstrip(u'\x00')
            except UnicodeDecodeError:
                # If it's UTF we may need to strip away some null characters before decoding.
                if code_page in ('utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be'):
                    decoded_data = self.decoded_data
                    while decoded_data and decoded_data[-1] == b'\x00':
                        try:
                            decoded_data = decoded_data[:-1]
                            output = decoded_data.decode(code_page).rstrip(u'\x00')
                        except UnicodeDecodeError:
                            continue
                        break  # successfully decoded
                    else:
                        continue
                # otherwise the code page isn't correct.
                else:
                    continue

            score = self._num_raw_bytes(output)
            if not best_output or score < best_score:
                best_score = score
                best_output = output
                best_code_page = code_page

        if best_output:
            self.code_page = best_code_page
            return best_output

        return u''


class EncodedStackString(EncodedString):
    """
    Variant of EncodedString that represents a string built from the stack.
    """

    def __init__(
            self, frame_id=None, stack_offset=None, memory_ptr=None, string_reference=INVALID,
            size=INVALID, offset=INVALID, key=INVALID):
        super(EncodedStackString, self).__init__(INVALID, string_reference, size, offset, key)
        # Frame ID and Stack Offset are optional because it's not always easy to calculate this.
        self.frame_id = frame_id
        self.stack_offset = stack_offset
        # TODO: Remove memory_ptr, it should be manually added by the decoder after initialization
        # if they want to use it.
        # Optional extra argument to keep track of pointer to string in memory when using function_tracingutils.
        self.memory_ptr = memory_ptr

    def _compare_key(self):
        # Sort by where it was found and then by offset within stack.
        return self.string_reference, self.frame_id, self.stack_offset, self.decoded_data

    def report(self):
        """
        General display format.

        :return unicode:
            A unicode string with the string's frame ID and stack offset,
            reference EA (where applicable), and the decoded value (where applicable).
        """
        text = u''
        if self.frame_id:
            text += u'Frame ID: 0x%X\n' % self.frame_id
        if self.stack_offset:
            text += u'Stack Offset: 0x%X\n' % self.stack_offset
        text += super(EncodedStackString, self).report()
        return text

    @property
    def xrefs_to(self):
        """
        Retrieves the xrefs to the stack variable.

        NOTE: This code is very SWIGGY because IDA did not properly expose this functionality.

        :raises ValueError: if frame_id, stack_offset, or string_reference was not provided.
            This is needed to determine what function to use.
        """
        if self._xrefs_to is None:
            if not self.string_reference:
                raise ValueError('Unable to get xrefs without string_reference.')
            if not (self.frame_id and self.stack_offset):
                raise ValueError('Unable to get xrefs without frame_id and stack_offset')
            xrefs = idaapi.xreflist_t()
            frame = idaapi.get_frame(self.frame_id)
            func = idaapi.get_func(self.string_reference)
            member = idaapi.get_member(frame, self.stack_offset)
            idaapi.build_stkvar_xrefs(xrefs, func, member)
            self._xrefs_to = [ref.ea for ref in xrefs]
        return self._xrefs_to

    def rename(self, name=None):
        """
        Renames (and comments) the string variable in IDA.

        :param str name: New name to given encoded string. (defaults to decoded_string)
        """
        name = name or self.display_name
        if not name:
            append_debug(
                'Unable to rename encoded string due to no decoded string: {!r}'.format(self),
                log_token='[!]')

        # Set name and comment in stack variable.
        comment = '"{}"'.format(name[:self._MAX_COMMENT_LENGTH])
        if len(name) > self._MAX_COMMENT_LENGTH:
            comment += ' (truncated)'
        if self.frame_id and self.stack_offset:
            idc.SetMemberComment(self.frame_id, self.stack_offset, comment, repeatable=1)
            var_name = re.sub('[^_$?@0-9A-Za-z]', '_', name[:self._MAX_NAME_LENGTH])  # Replace invalid characters
            if not var_name:
                raise ValueError('Unable to calculate var_name for : {!r}'.format(self))
            var_name = 'a' + var_name.capitalize()
            idc.SetMemberName(self.frame_id, self.stack_offset, var_name)

        # Add a comment where the string is being used.
        if self.string_reference:
            idc.MakeRptCmt(self.string_reference, comment)

    def patch(self, fill_char=None, define=True):
        """Does nothing, patching is not a thing for stack strings."""
        pass

    def get_bytes(self):
        """
        Get self.size bytes at self.string_location.

        :returns: self.size bytes at self.string_location

        :raises ValueError: Size was not valid.
        """
        # It's impossible to get bytes directly for stack strings.
        return INVALID

    def calc_size(self, width=1):
        raise ValueError('Size calculation cannot be done for stack strings.')


class StringTracer(object):
    """
    Description:
        An object to hold tracing info for one offset at a time.

        This abstract class doesn't do anything, but attempts to define fields that are
        almost always used. If a field is not used, it is recommended to set it to UNUSED.

    Fields:
        initial_offset - The EA the searching starts at. This is usually a yara match EA or a
                         func_t.startEA. (<initial_offset> must be within a function.)
        func_ea - The startEA of the function containing the initial_offset.
        string_location - The EA the encoded string starts at. Defaults to idc.BADADDR.
        string_reference - The EA from which the encoded string is referenced. Defaults to
                           idc.BADADDR.
        encoded_strings - Storage for the encoded string(s) found by search. Defaults to [].
        size - The size of the string. Defaults to INVALID.

    Input:
        initial_offset - Required. We have to start somewhere.
        identifier - The id of the YARA rule that this Tracer is based off of

    Throws:
        AttributeError - There was no function at initial_offset.
    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, initial_offset, identifier=UNUSED):
        super(StringTracer, self).__init__()
        self.initial_offset = initial_offset
        self.identifier = identifier
        if self.initial_offset != UNUSED:
            if not idaapi.get_func(initial_offset):
                raise AttributeError("No function at 0x%X" % initial_offset)
            self.func_ea = idaapi.get_func(initial_offset).startEA
        else:
            self.func_ea = INVALID
        self.string_location = idc.BADADDR
        self.string_reference = idc.BADADDR
        self.encoded_strings = []
        self.size = INVALID

    @abc.abstractmethod
    def search(self):
        """
        Description:
            Attempts to identify the string location based on the address passed in the constructor.

            A reminder: abc doesn't care about your parameters matching this prototype, just the name,
            so using **kwargs on search is fine. However, because find_encoded_strings is calling
            this function, there may be no *args as it wouldn't know what to pass. Assuming you don't
            edit this file's code, it may be useful to simply wrap your actual search function with this
            search function in some cases.

        Output:
            True if the encoded string offset is identified, False if the encoded string offset
            is not identified.
        """
        pass


def get_encoded_strings():
    """ Use responsibly """
    return ENCODED_STRINGS


def get_decoded_strings():
    """ Use responsibly """
    return DECODED_STRINGS


def is_valid_ea(ea):
    """
    Description:
        Returns true for valid EAs, False for invalid ones.

    Input:
        ea - The EA to check

    Output:
        True if the EA is valid, False if it is not
    """
    return ea not in [INVALID, UNUSED, idc.BADADDR] and idc.MinEA() <= ea <= idc.MaxEA()


def split_decoded_string(decoded_string, identify=False):
    """
    Description:
        Given a single EncodedString, split it into multiple.
        By default, it is split on \x00.
        The original EncodedString is not modified.

    Input:
        decoded_string - The EncodedString to split
        identify - When True, define the resultant strings in the IDB

    Output:
        A list of EncodedStrings.
    """
    '''
    Normally, we have either a block of either all one byte characters OR all two byte characters,
    however with some families, the blocks have utf-8 and utf-16 mixed together. When the block is all one
    byte characters, we just split the block on \x00 and are done with it. Likewise, when the block is all
    two byte characters, we just split the block on \x00\x00 and are done with it. For mixed blocks,
    we can't just split on one or the other. If we were to split on \x00 to find the single byte character strings,
    we would of course completely destroy the two byte character strings. Therefore, we effectively tokenize
    the block on \x00\x00 and process each sub-block separately.

    To process a block, we start from its end (which is guaranteed to be either \x00\x00 or the end of
    the buffer, which might as well be \x00) and work our way forward. We initially start by finding
    the rightmost \x00, which is either the end of the previous single byte character string or the
    first byte in a two byte character. To determine if the \x00 is part of two byte character string,
    we step back two characters. We keep stepping back as long as we keep landing
    on \x00. Once we hit a character that's not \x00, we undo our last step (and because we don't want
    to grab a leading \x00, step one more character, for a total of 3 characters not the 2 we have
    been stepping).

    Wherever we are now is the beginning of the string, which continues up until the index from which
    we searched for the rightmost \x00 earlier. We continue like this until we run into the front of
    the current block and then process the next block, repeating until we hit the end of the buffer.
    '''
    results = []
    section_start = 0
    section_end = decoded_string.decoded_data.find('\x00\x00')
    if section_end == -1:
        section_end = decoded_string.size
    string_end = section_end

    while True:
        while True:
            # Determine where the next individual string starts
            string_start = decoded_string.decoded_data[section_start: string_end].rfind('\x00')
            if string_start != -1:
                string_start += section_start
                while string_start >= section_start and decoded_string.decoded_data[string_start] == '\x00':
                    string_start -= 2
                if string_start > section_start:
                    string_start += 3  # last step +1 to skip \x00
                elif string_start < section_start:
                    # The leftmost string in the section, so don't step past section_start
                    string_start = section_start
            else:
                string_start = section_start  # The leftmost string in the block

            # Now that we have a string_start and string_end, we can carve the string
            new_decoded_string = decoded_string.decoded_data[string_start: string_end]
            if new_decoded_string:
                # Since we're using \x00 as the delimiter, we need to add \x00 to the end
                new_decoded_string += '\x00'
                new_string = copy.copy(decoded_string)
                new_string.decoded_data = new_decoded_string
                new_string.size = len(new_decoded_string)
                new_string.offset = string_start
                results.append(new_string)

            if string_start == section_start:
                break
            else:
                # -1 to skip \x00 for searching (which necessitates adding it back above)
                string_end = string_start - 1

        # We've processed a full section, advance to the next section
        if section_end == decoded_string.size:
            break
        else:
            section_start = section_end + 2
            # Skip blocks of \x00
            while section_start < decoded_string.size and decoded_string.decoded_data[section_start] == '\x00':
                section_start += 1
            section_end = decoded_string.decoded_data[section_start:].find('\x00\x00')
            if section_end == -1:  # The rightmost section in the block
                section_end = decoded_string.size
            else:
                section_end += section_start
            string_end = section_end

    results.sort(key=lambda decoded_string: decoded_string.startEA)

    if identify:
        for new_string in results:
            new_string.define()

    return results


def find_unrefd_encoded_strings(encoded_string, delimiter=None):
    """
    Description:
        Given a known EncodedString, find unreferenced encoded strings before the next ref.
        By default, the delimiter is the null terminator.
        The original EncodedString is not modified.

    Input:
        encoded_string - The EncodedString to start from
        delimiter - The character(s) to split on (default is null terminator)

    Output:
        A list of new EncodedStrings (not including the original)
    """
    if delimiter is None:
        delimiter = '\x00\x00' if isinstance(encoded_string.decoded_string, unicode) else '\x00'

    results = []
    index = encoded_string.string_location + encoded_string.size

    while not list(idautils.XrefsTo(index, idaapi.XREF_ALL)):
        # Consume bytes while we continue to find the delimiter
        if idc.GetManyBytes(index, len(delimiter)) == delimiter:
            if len(delimiter) > 1:
                # if the delimiter is multiple bytes, we could have stepped over a ref
                # in the middle of the delimiter
                if any(bool(list(idautils.XrefsTo(index + i, idaapi.XREF_ALL))) for i in xrange(len(delimiter))):
                    break
                else:
                    index += len(delimiter)
            else:
                index += len(delimiter)
        else:
            # For cases where the delimiter has repeated values (like unicode null),
            # step until the delimiter is right aligned
            while not list(idautils.XrefsTo(index, idaapi.XREF_ALL)) and \
                    idc.GetManyBytes(index + 1, len(delimiter)) == delimiter:
                index += 1
            # Technically we need to check this again to be super safe
            if list(idautils.XrefsTo(index, idaapi.XREF_ALL)):
                break
            start = index

            # Consume non-delimiter bytes until we encounter another delimiter or a ref
            index += 1
            while not list(idautils.XrefsTo(index, idaapi.XREF_ALL)) and \
                    idc.GetManyBytes(index, len(delimiter)) != delimiter:
                index += 1

            new_string = copy.copy(encoded_string)
            new_string.offset = start - encoded_string.string_location
            new_string.size = index - start
            new_string.get_bytes()
            results.append(new_string)

    return results


def find_encoded_strings_inline(matches, Tracer, **kwargs):
    """
    For each yara match, attempt to find encoded strings.

    Input:
        matches - A list of yara matches (ea, identifier)
        Tracer - A pointer to an implementation of StringTracer.
        **kwargs - kwargs to be passed to Tracer's constructor.

    Output:
        A list of EncodedStrings.
    """
    encoded_strings = []
    for ea, identifier in matches:
        try:
            tracer = Tracer(ea, identifier, **kwargs)
            if tracer.search():
                encoded_strings.extend(tracer.encoded_strings)
            else:
                append_debug('Failed to find strings at 0x%X' % ea)
        except AttributeError:
            append_debug('Error tracing at 0x%X' % ea)
    return encoded_strings


def find_encoded_strings(funcs, Tracer, **kwargs):
    """
    Description:
        For each ref for each function, attempt to find encoded strings.

    Input:
        funcs - A list of functions. Must have an xrefs_to field.
        Tracer - A pointer to an implementation of StringTracer.
        **kwargs - kwargs to be passed to Tracer's constructor.

    Output:
        A list of EncodedStrings.
    """
    encoded_strings = []
    for func in funcs:
        for ref in func.xrefs_to:
            if idc.SegName(ref) == '.pdata':
                append_debug('Segment .pdata for ref 0x%08x is not a relevant code segment and will be skipped' % ref)
            else:
                try:
                    tracer = Tracer(ref, func.identifier, **kwargs)
                    if tracer.search():
                        encoded_strings.extend(tracer.encoded_strings)
                    else:
                        append_debug('Failed to find strings at 0x%X' % ref)
                except AttributeError:
                    append_debug(
                        'No function exists at 0x%X. Create a function at this location to obtain strings.' % ref)
    return encoded_strings


def decode_strings(encoded_strings, decode):
    """
    Description:
        For each encoded_string entry in encoded_strings, decode the data using the provided
        decode function.

    Input:
        encoded_strings - A list of EncodedStrings.
        decode - A pointer to a function that handles decoding.

    Output:
        A list of successfully decoded EncodedStrings.
    """
    decoded_strings = []
    for encoded_string in encoded_strings:
        if encoded_string.encoded_data == INVALID:
            append_debug('Unable to find string at: 0x%X' % encoded_string.string_location)
            continue
        encoded_string.decoded_string = decode(encoded_string)
        if encoded_string.decoded_string:  # Allow decoders to abort/fail quietly
            decoded_strings.append(encoded_string)
        else:
            append_debug('Failed to decode string: {!r}'.format(encoded_string), '[!]')
    return decoded_strings


def _yara_callback(data):
    """
    Description:
        Generic yara callback.

    Input:
        As defined by YARA. See YARA's documentation for more info.

    Output:
        A list of tuples: (offset, identifier)
    """
    if not data['matches']:
        return False

    for datum in data['strings']:
        _YARA_MATCHES.append((idc.ItemHead(idaapi.get_fileregion_ea(datum[0])), datum[1]))

    return yara.CALLBACK_CONTINUE


def generic_run_yara(rule_text, callback_func=_yara_callback):
    """
    Description:
        Applies yara rule and returns raw results. Clear the matches each time to prevent
        duplicates.

    Input:
        rule_text - A string containing a YARA rule
        callback_func - A pointer to the callback function for YARA's matching to use

    Output:
        Returns a list of YARA's match results with items (location, description)
    """
    global _YARA_MATCHES
    _YARA_MATCHES = []

    yara.compile(source=rule_text).match(INPUT_FILE_PATH, callback=callback_func)
    return _YARA_MATCHES


def yara_find_decode_functions(rule_text, func_name=None, callback_func=_yara_callback):
    """
    Description:
        Use yara to find the string decode functions, rename them, and return the SuperFunc_ts.
        Clear the matches each time to prevent duplicates.

    Input:
        rule-text - A string containing a YARA rule
        func_name - The name to be applied to the found function(s). No name will be applied
                    if func_name = None.
        callback_func - A pointer to the callback function for YARA's matching to use

    Output:
        A list of SuperFunc_t objects.

    Throws:
        RuntimeError - Assumes that there's no point in continuing if there is no YARA match
                       and that we were expecting a YARA match, so error in that case.
    """
    global _YARA_MATCHES
    _YARA_MATCHES = []

    if not yara.compile(source=rule_text).match(INPUT_FILE_PATH, callback=callback_func):
        raise RuntimeError("The provided yara rule failed to match!")

    return make_superfunc_t_from_matches(_YARA_MATCHES, func_name)


def make_superfunc_t_from_matches(matches, func_name=None):
    """
    Description:
        Makes a SuperFunc_t object for each yara match

    Input:
        matches - A list of (ea, identifier) all of which are inside a function.
        func_name - If a non-None name is provided, rename the functions with that name.

    Output:
        A list of decoded functions. Renames those functions if a name is provided.
    """
    decode_funcs = set()
    for ea, identifier in matches:
        if ea == idc.BADADDR:
            continue
        func = SuperFunc_t(ea, identifier)
        if func_name is not None:
            func.rename(func_name)
        decode_funcs.add(func)
    return list(decode_funcs)


def output_strings(decoded_strings, dedup=True, rename=True, patch=False):
    """
    1. Outputs the decoded string data to the console
    2. Prints decoded string info to the console.
    3. Comments and renames the variables if requested.
    4. Patches encoded string with decoded string if requested.

    :param decoded_strings: The list of decoded EncodedStrings
    :param bool dedup: Whether to dedup strings.
    :param bool rename: Whether to rename strings. (default True)
    :param bool patch: Whether to patch strings. (default False)

    :return: list of decoded string
    """
    if dedup:
        decoded_strings = set(decoded_strings)

    string_list = []
    for decoded_string in sorted(decoded_strings):
        string_list.append(str(decoded_string))
        # FIXME: Even though we strip nulls in __unicode__(), there still seems to be some strings
        # with null characters seeping through.
        append_string(unicode(decoded_string).rstrip(u'\x00'))

        print '\n'
        display = decoded_string.report()
        print display

        if rename:
            decoded_string.rename()
        if patch:
            decoded_string.patch()

    return string_list


def patch_decoded(decoded_strings, define=True):
    """
    Description:
        Patches the bytes at each encoded string location with the decoded string value.
        Assumes null termination.

    Input:
        decoded_strings - A list of successfully decoded strings.
        define - When True, defines a string in the IDB

    Output:
        Makes a string in IDA at the string's start_location
    """
    warnings.warn(
        'patch_decoded() is deprecated. Please use .patch() directly on the EncodedString object.',
        DeprecationWarning)
    for decoded_string in sorted(decoded_strings, key=lambda string: string.string_location):
        decoded_string.patch(define=define)


def define_string(decoded_string):
    """
    Defines a string object in the IDB for the provided string.

    Input:
        decoded_string - The EncodedString object to define in IDA
    """
    warnings.warn(
        'define_string() is deprecated. Please use .define() directly on the EncodedString object.',
        DeprecationWarning)
    decoded_string.define()


def find_input_file():
    """
    Description:
        Check whether or not IDA knows where the original file used to create the IDB is.
        If IDA doesn't know, check the IDA's directory for the file.

    Output:
        Returns True if the input file was located, False if it was not.
    """
    global INPUT_FILE_PATH
    ida_path = INPUT_FILE_PATH
    if not os.path.exists(ida_path):
        # If IDA does not know, check if the (correct) file is sitting next to the IDB.
        local_path = os.path.join(idautils.GetIdbDir(), idc.GetInputFile())
        if os.path.exists(local_path) and \
                hashlib.md5(open(local_path, 'rb').read()).hexdigest().upper() == idc.GetInputMD5():
            INPUT_FILE_PATH = local_path
            append_debug('Guessed the input file path: ' + INPUT_FILE_PATH)
            append_debug('IDA thought it was:          ' + ida_path)
            return True
        else:
            return False
    else:
        return True


def string_decoder_main(yara_rule, Tracer, decode, patch=True, func_name='string_decode_function', inline=False):
    """
    Description:
        If you are going to use this file's workflow as is, this is the entry point. This supports the majority
        of decode function xref based decoding algorithms (the 'older' way) and inline decoding algorithms
        (the 'newer' way).

    Input:
        First, you'll need to provide a YARA rule. The rule can either be passed as a string or in a file.
        If it is in a file, be sure to set is_yara_file = True. Also note that when YARA finds the decoder
        function, it will attempt to rename it:
            func_name = '' will reset the name to the default name it had when IDA built the IDB
            func_name = None will prevent this script from renaming it

        Second, you need to implement the StringTracer class above to handle your malware's particular
        way of loading the encrypted string. Pass a pointer to this class as the second parameter.

        Third, you need to implement the decoding algorithm and pass a pointer to the entry function
        as the third parameter.

        It is also important to have the original file in the correct location for YARA rule application.
        The IDB contains the path of original file from when the IDB was created, but if the file isn't there,
        this script will check the IDB's current directory for a file with the same name and same md5.

        This script will also patch the encoded string with the decoded one unless patch = False.

    Throws:
        AttributeError - see StringTracer, SuperFunc_t
        OSError - The provided YARA file could not be opened.
        RuntimeError - see _yara_find_decode_functions
        TypeError - The provided Tracer does not extend StringTracer.
        ValueError - Could not find the file used to create the IDB.
                   - see EncodedString.get_bytes
    """
    global ENCODED_STRINGS
    global DECODED_STRINGS

    # We could just check if there is a 'search' method, but handle it this way to enforce conventions.
    if not issubclass(Tracer, StringTracer):
        append_debug("Tracer does not extend StringTracer!")
        return

    # Check that IDA actually knows where the original input file is.
    if not find_input_file():
        append_debug("Unable to locate the file used to create the IDB: " + INPUT_FILE_PATH)
        return

    # Do the decoding.
    try:
        if inline:
            matches = generic_run_yara(yara_rule)
            ENCODED_STRINGS = find_encoded_strings_inline(matches, Tracer)
        else:
            decode_functions = yara_find_decode_functions(yara_rule, func_name)
            ENCODED_STRINGS = find_encoded_strings(decode_functions, Tracer)
        ENCODED_STRINGS = decode_strings(ENCODED_STRINGS, decode)
        string_list = output_strings(ENCODED_STRINGS)
        if patch:
            patch_decoded(ENCODED_STRINGS)
        return string_list
    except RuntimeError:
        append_debug("The provided YARA rule failed to match. No strings can be decrypted for this YARA rule.")
        return


idc.Wait()  # Force wait on import just to be sure
