import copy
import re
import os
from fortls.jsonrpc import path_to_uri
WORD_REGEX = re.compile(r'[a-z_][a-z0-9_]*', re.I)
CLASS_VAR_REGEX = re.compile(r'(TYPE|CLASS)[ ]*\(', re.I)
DEF_KIND_REGEX = re.compile(r'([a-z]*)[ ]*\((?:KIND|LEN)?[ =]*([a-z_][a-z0-9_]*)', re.I)
# Keyword identifiers
KEYWORD_LIST = [
    'pointer',
    'allocatable',
    'optional',
    'public',
    'private',
    'nopass',
    'target',
    'save',
    'parameter',
    'contiguous',
    'deferred',
    'dimension',
    'intent',
    'pass',
    'pure',
    'elemental',
    'recursive'
]
KEYWORD_ID_DICT = {keyword: ind for (ind, keyword) in enumerate(KEYWORD_LIST)}
# Type identifiers
MODULE_TYPE_ID = 1
SUBROUTINE_TYPE_ID = 2
FUNCTION_TYPE_ID = 3
CLASS_TYPE_ID = 4
INTERFACE_TYPE_ID = 5
VAR_TYPE_ID = 6
METH_TYPE_ID = 7
BLOCK_TYPE_ID = 8
SELECT_TYPE_ID = 9
DO_TYPE_ID = 10
WHERE_TYPE_ID = 11
IF_TYPE_ID = 12
ASSOC_TYPE_ID = 13
ENUM_TYPE_ID = 14
sort_keywords = True


def set_keyword_ordering(sorted):
    global sort_keywords
    sort_keywords = sorted


def map_keywords(keywords):
    mapped_keywords = []
    keyword_info = {}
    for keyword in keywords:
        keyword_prefix = keyword.split('(')[0].lower()
        keyword_ind = KEYWORD_ID_DICT.get(keyword_prefix)
        if keyword_ind is not None:
            mapped_keywords.append(keyword_ind)
            if keyword_prefix in ('intent', 'dimension', 'pass'):
                keyword_substring = get_paren_substring(keyword)
                if keyword_substring is not None:
                    keyword_info[keyword_prefix] = keyword_substring
    if sort_keywords:
        mapped_keywords.sort()
    return mapped_keywords, keyword_info


def get_keywords(keywords, keyword_info={}):
    keyword_strings = []
    for keyword_id in keywords:
        string_rep = KEYWORD_LIST[keyword_id]
        addl_info = keyword_info.get(string_rep)
        string_rep = string_rep.upper()
        if addl_info is not None:
            string_rep += '({0})'.format(addl_info)
        keyword_strings.append(string_rep)
    return keyword_strings


def intersect_lists(l1, l2):
    tmp_list = []
    for val1 in l1:
        if l2.count(val1) > 0:
            tmp_list.append(val1)
    return tmp_list


def get_paren_substring(test_str):
    i1 = test_str.find('(')
    i2 = test_str.rfind(')')
    if i1 > -1 and i2 > i1:
        return test_str[i1+1:i2]
    else:
        return None


def get_use_tree(scope, use_dict, obj_tree, only_list=[]):
    # Add recursively
    for use_stmnt in scope.use:
        use_mod = use_stmnt[0]
        if len(only_list) == 0:
            merged_use_list = use_stmnt[1]
        elif len(use_stmnt[1]) == 0:
            merged_use_list = only_list
        else:
            merged_use_list = intersect_lists(only_list, use_stmnt[1])
            if len(merged_use_list) == 0:
                continue
        if use_mod in obj_tree:
            if use_mod in use_dict:
                old_len = len(use_dict[use_mod])
                if (old_len > 0) and (len(merged_use_list) > 0):
                    for only_name in use_stmnt[1]:
                        if use_dict[use_mod].count(only_name) == 0:
                            use_dict[use_mod].append(only_name)
                else:
                    use_dict[use_mod] = []
                # Skip if we have already visited module with the same only list
                if old_len == len(use_dict[use_mod]):
                    continue
            else:
                use_dict[use_mod] = merged_use_list
            use_dict = get_use_tree(obj_tree[use_mod][0], use_dict, obj_tree, merged_use_list)
    return use_dict


def find_in_scope(scope, var_name, obj_tree, interface=False):
    def check_scope(local_scope, var_name_lower, filter_public=False):
        for child in local_scope.get_children():
            if child.name.startswith("#GEN_INT"):
                tmp_var = check_scope(child, var_name_lower, filter_public)
                if tmp_var is not None:
                    return tmp_var
            if filter_public:
                if (child.vis < 0) or ((local_scope.def_vis < 0) and (child.vis <= 0)):
                    continue
            if child.name.lower() == var_name_lower:
                return child
        return None
    #
    var_name_lower = var_name.lower()
    # Check local scope
    tmp_var = check_scope(scope, var_name_lower)
    if tmp_var is not None:
        return tmp_var
    # Setup USE search
    use_dict = get_use_tree(scope, {}, obj_tree)
    # Look in found use modules
    for use_mod, only_list in use_dict.items():
        use_scope = obj_tree[use_mod][0]
        # Module name is request
        if use_mod.lower() == var_name_lower:
            return use_scope
        # Filter children by only_list
        if len(only_list) > 0:
            if var_name_lower not in only_list:
                continue
        tmp_var = check_scope(use_scope, var_name_lower, filter_public=True)
        if tmp_var is not None:
            return tmp_var
    # Only search local and imported names for interfaces
    if interface:
        in_import = False
        for use_line in scope.use:
            if use_line[0].startswith('#import'):
                if use_line[1].count(var_name_lower) > 0:
                    in_import = True
                    break
        if not in_import:
            return None
    # Check parent scopes
    if scope.parent is not None:
        tmp_var = find_in_scope(scope.parent, var_name, obj_tree)
        if tmp_var is not None:
            return tmp_var
    # Check ancestor scopes
    for ancestor in scope.get_ancestors():
        tmp_var = find_in_scope(ancestor, var_name, obj_tree)
        if tmp_var is not None:
            return tmp_var
    return None


def find_in_workspace(obj_tree, query, filter_public=False, exact_match=False):
    def add_children(mod_obj, query):
        tmp_list = []
        for child_obj in mod_obj.get_children(filter_public):
            if child_obj.name.lower().find(query) >= 0:
                tmp_list.append(child_obj)
        return tmp_list
    matching_symbols = []
    query = query.lower()
    for (_, obj_packed) in obj_tree.items():
        top_obj = obj_packed[0]
        top_uri = obj_packed[1]
        if top_uri is not None:
            if top_obj.name.lower().find(query) > -1:
                matching_symbols.append(top_obj)
            if top_obj.get_type() == MODULE_TYPE_ID:
                matching_symbols += add_children(top_obj, query)
    if exact_match:
        filtered_symbols = []
        n = len(query)
        for symbol in matching_symbols:
            if len(symbol.name) == n:
                filtered_symbols.append(symbol)
        matching_symbols = filtered_symbols
    return matching_symbols


def find_word_in_line(line, word):
    i0 = -1
    for poss_name in WORD_REGEX.finditer(line):
        if poss_name.group() == word:
            i0 = poss_name.start()
            break
    return i0, i0 + len(word)


def build_diagnostic(sline, message, severity=1, eline=None, file_contents=None, find_word=None,
                     related_path=None, related_line=None, related_message=""):
    if eline is None:
        eline = sline
    i0 = i1 = 0
    if (file_contents is not None) and (find_word is not None):
        i0, i1 = find_word_in_line(file_contents[sline].lower(), find_word.lower())
    diag = {
        "range": {
            "start": {"line": sline, "character": i0},
            "end": {"line": eline, "character": i1}
        },
        "message": message,
        "severity": severity
    }
    if (related_path is not None) and (related_line is not None):
        diag["relatedInformation"] = [{
            "location": {
                "uri": path_to_uri(related_path),
                "range": {
                    "start": {"line": related_line, "character": 0},
                    "end": {"line": related_line, "character": 0}
                }
            },
            "message": related_message
        }]
    return diag


class fortran_diagnostic:
    def __init__(self, sline, message, severity=1, find_word=None):
        self.sline = sline
        self.message = message
        self.severity = severity
        self.find_word = find_word
        self.has_related = False
        self.related_path = None
        self.related_line = None
        self.related_message = None

    def add_related(self, path, line, message):
        self.has_related = True
        self.related_path = path
        self.related_line = line
        self.related_message = message

    def build(self, file_obj):
        schar = echar = 0
        if self.find_word is not None:
            _, curr_line, forward_lines = file_obj.get_code_line(self.sline, backward=False)
            schar, echar = find_word_in_line(curr_line.lower(), self.find_word.lower())
            if schar < 0:
                for (i, line) in enumerate(forward_lines):
                    schar, echar = find_word_in_line(line.lower(), self.find_word.lower())
                    if schar >= 0:
                        self.sline += i+1
                        break
        if schar < 0:
            schar = echar = 0
        diag = {
            "range": {
                "start": {"line": self.sline, "character": schar},
                "end": {"line": self.sline, "character": echar}
            },
            "message": self.message,
            "severity": self.severity
        }
        if self.has_related:
            diag["relatedInformation"] = [{
                "location": {
                    "uri": path_to_uri(self.related_path),
                    "range": {
                        "start": {"line": self.related_line, "character": 0},
                        "end": {"line": self.related_line, "character": 0}
                    }
                },
                "message": self.related_message
            }]
        return diag


class fortran_obj:
    def __init__(self):
        self.vis = 0
        self.def_vis = 0
        self.doc_str = None
        self.parent = None
        self.eline = -1
        self.implicit_vars = None

    def set_default_vis(self, new_vis):
        self.def_vis = new_vis

    def set_visibility(self, new_vis):
        self.vis = new_vis

    def set_parent(self, parent_obj):
        self.parent = parent_obj

    def add_doc(self, doc_str):
        self.doc_str = doc_str

    def update_fqsn(self, enc_scope=None):
        return None

    def end(self, line_number):
        self.eline = line_number

    def resolve_inherit(self, obj_tree):
        return None

    def resolve_link(self, obj_tree):
        return None

    def get_type(self):
        return -1

    def get_desc(self):
        return 'unknown'

    def get_snippet(self, name_replace=None, drop_arg=-1):
        return None, None

    def get_documentation(self):
        return self.doc_str

    def get_hover(self, long=False, include_doc=True, drop_arg=-1):
        return None, False

    def get_signature(self, drop_arg=-1):
        return None, None, None

    def get_interface(self, name_replace=None, drop_arg=-1, change_strings=None):
        return None

    def get_children(self, public_only=False):
        return []

    def get_ancestors(self):
        return []

    def get_diagnostics(self):
        return []

    def get_implicit(self):
        if self.parent is None:
            return self.implicit_vars
        else:
            parent_implicit = self.parent.get_implicit()
            if (self.implicit_vars is not None) or (parent_implicit is None):
                return self.implicit_vars
            return parent_implicit

    def get_actions(self, sline, eline):
        return None

    def is_optional(self):
        return False

    def is_mod_scope(self):
        return False

    def is_callable(self):
        return False

    def is_external_int(self):
        return False

    def is_abstract(self):
        return False

    def req_named_end(self):
        return False

    def check_valid_parent(self):
        return True

    def check_definition(self, obj_tree, known_types={}, interface=False):
        return None, known_types


class fortran_scope(fortran_obj):
    def __init__(self, file_obj, line_number, name):
        self.base_setup(file_obj, line_number, name)

    def base_setup(self, file_obj, sline, name, keywords=[]):
        self.file = file_obj
        self.sline = sline
        self.eline = sline
        self.name = name
        self.children = []
        self.members = []
        self.use = []
        self.keywords = keywords
        self.inherit = None
        self.parent = None
        self.vis = 0
        self.def_vis = 0
        self.contains_start = None
        self.doc_str = None
        self.implicit_vars = None
        self.implicit_line = None
        if file_obj.enc_scope_name is not None:
            self.FQSN = file_obj.enc_scope_name.lower() + "::" + self.name.lower()
        else:
            self.FQSN = self.name.lower()

    def add_use(self, use_mod, line_number, only_list=[]):
        lower_only = []
        for only in only_list:
            lower_only.append(only.lower())
        self.use.append([use_mod.lower(), lower_only, line_number])

    def set_inherit(self, inherit_type):
        self.inherit = inherit_type

    def resolve_inherit(self, obj_tree):
        for child in self.children:
            child.resolve_inherit(obj_tree)

    def resolve_link(self, obj_tree):
        for child in self.children:
            child.resolve_link(obj_tree)

    def set_parent(self, parent_obj):
        self.parent = parent_obj

    def set_implicit(self, implicit_flag, line_number):
        self.implicit_vars = implicit_flag
        self.implicit_line = line_number

    def mark_contains(self, line_number):
        if self.contains_start is not None:
            raise ValueError
        self.contains_start = line_number

    def add_child(self, child):
        self.children.append(child)
        child.set_parent(self)

    def update_fqsn(self, enc_scope=None):
        if enc_scope is not None:
            self.FQSN = enc_scope.lower() + "::" + self.name.lower()
        else:
            self.FQSN = self.name.lower()
        for child in self.children:
            child.update_fqsn(self.FQSN)

    def add_member(self, member):
        self.members.append(member)

    def get_children(self, public_only=False):
        if public_only:
            pub_children = []
            for child in self.children:
                if (child.vis < 0) or ((self.def_vis < 0) and (child.vis <= 0)):
                    continue
                if child.name.startswith("#GEN_INT"):
                    pub_children.append(child)
                    continue
                pub_children.append(child)
            return pub_children
        else:
            return self.children

    def check_definitions(self, obj_tree):
        """Check for definition errors in scope"""
        FQSN_dict = {}
        for child in self.children:
            # Check other variables in current scope
            if child.FQSN in FQSN_dict:
                if child.sline < FQSN_dict[child.FQSN]:
                    FQSN_dict[child.FQSN] = child.sline - 1
            else:
                FQSN_dict[child.FQSN] = child.sline - 1
        #
        contains_line = -1
        if self.get_type() in (MODULE_TYPE_ID, SUBROUTINE_TYPE_ID, FUNCTION_TYPE_ID):
            if self.contains_start is None:
                contains_line = self.eline
            else:
                contains_line = self.contains_start
        # Get list of imported objects for interfaces
        is_interface = False
        if (self.parent is not None) and (self.parent.get_type() == INTERFACE_TYPE_ID):
            is_interface = True
        errors = []
        known_types = {}
        for child in self.children:
            line_number = child.sline - 1
            # Check for type definition in scope
            def_error, known_types = child.check_definition(
                obj_tree, known_types=known_types, interface=is_interface
            )
            if def_error is not None:
                errors.append(def_error)
            # Detect contains errors
            if (contains_line >= 0) and (child.get_type() in (SUBROUTINE_TYPE_ID, FUNCTION_TYPE_ID)):
                new_diag = fortran_diagnostic(
                    line_number, message='Subroutine/Function definition before CONTAINS statement',
                    severity=1
                )
                errors.append(new_diag)
            # Skip masking/double checks for interface members
            if (self.parent is not None) and (self.parent.get_type() == INTERFACE_TYPE_ID):
                continue
            # Check other variables in current scope
            if child.FQSN in FQSN_dict:
                if line_number > FQSN_dict[child.FQSN]:
                    new_diag = fortran_diagnostic(
                        line_number, message='Variable "{0}" declared twice in scope'.format(child.name),
                        severity=1, find_word=child.name
                    )
                    new_diag.add_related(path=self.file.path, line=FQSN_dict[child.FQSN], message='First declaration')
                    errors.append(new_diag)
                    continue
            # Check for masking from parent scope in subroutines, functions, and blocks
            if (self.parent is not None) and \
               (self.get_type() in (SUBROUTINE_TYPE_ID, FUNCTION_TYPE_ID, BLOCK_TYPE_ID)):
                parent_var = find_in_scope(self.parent, child.name, obj_tree)
                if parent_var is not None:
                    # Ignore if function return variable
                    if (self.get_type() == FUNCTION_TYPE_ID) and (parent_var.FQSN == self.FQSN):
                        continue
                    new_diag = fortran_diagnostic(
                        line_number, message='Variable "{0}" masks variable in parent scope'.format(child.name),
                        severity=2, find_word=child.name
                    )
                    new_diag.add_related(path=parent_var.file.path, line=parent_var.sline-1,
                                         message='First declaration')
                    errors.append(new_diag)
        return errors

    def check_use(self, obj_tree):
        errors = []
        last_use_line = -1
        for use_line in self.use:
            use_mod = use_line[0]
            last_use_line = max(last_use_line, use_line[2])
            if use_mod.startswith('#import'):
                if (self.parent is None) or (self.parent.get_type() != INTERFACE_TYPE_ID):
                    new_diag = fortran_diagnostic(
                        use_line[2]-1, message='IMPORT statement outside of interface', severity=1
                    )
                    errors.append(new_diag)
                continue
            if use_mod not in obj_tree:
                new_diag = fortran_diagnostic(
                    use_line[2]-1, message='Module "{0}" not found in project'.format(use_mod),
                    severity=3, find_word=use_mod
                )
                errors.append(new_diag)
        if (self.implicit_line is not None) and (last_use_line >= self.implicit_line):
            new_diag = fortran_diagnostic(
                self.implicit_line-1, message='USE statements after IMPLICIT statement',
                severity=1, find_word='IMPLICIT'
            )
            errors.append(new_diag)
        return errors

    def add_subroutine(self, interface_string, no_contains=False):
        edits = []
        line_number = self.eline - 1
        if (self.contains_start is None) and (not no_contains):
            first_sub_line = line_number
            for child in self.children:
                if child.get_type() in (SUBROUTINE_TYPE_ID, FUNCTION_TYPE_ID):
                    first_sub_line = min(first_sub_line, child.sline - 1)
            edits.append({
                "range": {
                    "start": {"line": first_sub_line, "character": 0},
                    "end": {"line": first_sub_line, "character": 0}
                },
                "newText": "CONTAINS\n"
            })
        edits.append({
            "range": {
                "start": {"line": line_number, "character": 0},
                "end": {"line": line_number, "character": 0}
            },
            "newText": interface_string + "\n"
        })
        return self.file.path, edits


class fortran_module(fortran_scope):
    def get_type(self):
        return MODULE_TYPE_ID

    def get_desc(self):
        return 'MODULE'

    def check_valid_parent(self):
        if self.parent is not None:
            return False
        return True


class fortran_program(fortran_module):
    def get_desc(self):
        return 'PROGRAM'


class fortran_submodule(fortran_module):
    def __init__(self, file_obj, line_number, name, ancestor_name=None):
        self.base_setup(file_obj, line_number, name)
        self.ancestor_name = ancestor_name
        self.ancestor_obj = None

    def get_desc(self):
        return 'SUBMODULE'

    def get_ancestors(self):
        if self.ancestor_obj is not None:
            great_ancestors = self.ancestor_obj.get_ancestors()
            if great_ancestors is not None:
                return [self.ancestor_obj] + great_ancestors
            return [self.ancestor_obj]
        return []

    def resolve_inherit(self, obj_tree):
        if self.ancestor_name is None:
            return
        if self.ancestor_name in obj_tree:
            self.ancestor_obj = obj_tree[self.ancestor_name][0]

    def resolve_link(self, obj_tree):
        # Link subroutine/function implementations to prototypes
        if self.ancestor_obj is None:
            return
        # Grab ancestor interface definitions (function/subroutine only)
        ancestor_interfaces = []
        for child in self.ancestor_obj.children:
            if child.get_type() == INTERFACE_TYPE_ID:
                for prototype in child.children:
                    prototype_type = prototype.get_type()
                    if (prototype_type == SUBROUTINE_TYPE_ID or prototype_type == FUNCTION_TYPE_ID) \
                       and prototype.is_mod_scope():
                        ancestor_interfaces.append(prototype)
        # Match interface definitions to implementations
        for prototype in ancestor_interfaces:
            for child in self.children:
                if (child.name.lower() == prototype.name.lower()) and (child.get_type() == prototype.get_type()):
                    prototype.resolve_link(obj_tree)
                    child.copy_interface(prototype)
                    break


class fortran_subroutine(fortran_scope):
    def __init__(self, file_obj, line_number, name, args="", mod_sub=False, keywords=[]):
        self.base_setup(file_obj, line_number, name, keywords=keywords)
        self.args = args.replace(' ', '').lower()
        self.args_snip = self.args
        self.arg_objs = []
        self.in_children = []
        self.missing_args = []
        self.mod_scope = mod_sub

    def is_mod_scope(self):
        return self.mod_scope

    def is_callable(self):
        return True

    def copy_interface(self, copy_source):
        # Copy arguments
        self.args = copy_source.args
        self.args_snip = copy_source.args_snip
        self.arg_objs = copy_source.arg_objs
        # Get current fields
        child_names = []
        for child in self.children:
            child_names.append(child.name.lower())
        # Import arg_objs from copy object
        self.in_children = []
        for child in copy_source.arg_objs:
            if child is None:
                continue
            if child.name.lower() not in child_names:
                self.in_children.append(child)

    def get_children(self, public_only=False):
        tmp_list = copy.copy(self.children)
        tmp_list.extend(self.in_children)
        return tmp_list

    def resolve_arg_link(self, obj_tree):
        if self.args == '':
            return
        arg_list = self.args.replace(' ', '').split(',')
        self.arg_objs = [None for arg in arg_list]
        check_objs = self.children
        for child in self.children:
            if child.is_external_int():
                check_objs += child.get_children()
        self.missing_args = []
        for child in check_objs:
            ind = -1
            for (i, arg) in enumerate(arg_list):
                if arg == child.name.lower():
                    ind = i
                    break
            if ind < 0:
                if child.keywords.count(KEYWORD_ID_DICT['intent']) > 0:
                    self.missing_args.append(child)
            else:
                self.arg_objs[ind] = child
                if child.is_optional():
                    arg_list[ind] = "{0}={0}".format(arg_list[ind])
            child.resolve_link(obj_tree)
        self.args_snip = ",".join(arg_list)

    def resolve_link(self, obj_tree):
        self.resolve_arg_link(obj_tree)

    def get_type(self):
        return SUBROUTINE_TYPE_ID

    def get_snippet(self, name_replace=None, drop_arg=-1):
        arg_list = self.args_snip.split(",")
        if (drop_arg >= 0) and (drop_arg < len(arg_list)):
            del arg_list[drop_arg]
        arg_snip = None
        if len(arg_list) > 0:
            place_holders = []
            for i, arg in enumerate(arg_list):
                opt_split = arg.split("=")
                if len(opt_split) > 1:
                    place_holders.append("{1}=${{{0}:{2}}}".format(i+1, opt_split[0], opt_split[1]))
                else:
                    place_holders.append("${{{0}:{1}}}".format(i+1, arg))
            arg_str = "({0})".format(", ".join(arg_list))
            arg_snip = "({0})".format(", ".join(place_holders))
        else:
            arg_str = "()"
        name = self.name
        if name_replace is not None:
            name = name_replace
        snippet = None
        if arg_snip is not None:
            snippet = name + arg_snip
        return name + arg_str, snippet

    def get_desc(self):
        return 'SUBROUTINE'

    def get_hover(self, long=False, include_doc=True, drop_arg=-1):
        sub_sig, _ = self.get_snippet(drop_arg=drop_arg)
        keyword_list = get_keywords(self.keywords)
        keyword_list.append("SUBROUTINE ")
        hover_array = [" ".join(keyword_list) + sub_sig]
        doc_str = self.get_documentation()
        if include_doc and (doc_str is not None):
            hover_array[0] += "\n" + doc_str
        if long:
            for (i, arg_obj) in enumerate(self.arg_objs):
                if (arg_obj is None) or (i == drop_arg):
                    continue
                arg_doc, _ = arg_obj.get_hover(include_doc=False)
                hover_array.append("{0} :: {1}".format(arg_doc, arg_obj.name))
                doc_str = arg_obj.get_documentation()
                if include_doc and (doc_str is not None):
                    hover_array += doc_str.splitlines()
        return "\n ".join(hover_array), long

    def get_signature(self, drop_arg=-1):
        arg_sigs = []
        arg_list = self.args.split(",")
        for (i, arg_obj) in enumerate(self.arg_objs):
            if i == drop_arg:
                continue
            if arg_obj is None:
                arg_sigs.append({"label": arg_list[i]})
            else:
                if arg_obj.is_optional():
                    label = "{0}={0}".format(arg_obj.name.lower())
                else:
                    label = arg_obj.name.lower()
                arg_sigs.append({
                    "label": label,
                    "documentation": arg_obj.get_hover()[0]
                })
        call_sig, _ = self.get_snippet()
        return call_sig, self.get_documentation(), arg_sigs

    def get_interface(self, name_replace=None, change_arg=-1, change_strings=None):
        sub_sig, _ = self.get_snippet(name_replace=name_replace)
        keyword_list = get_keywords(self.keywords)
        keyword_list.append("SUBROUTINE ")
        interface_array = [" ".join(keyword_list) + sub_sig]
        for (i, arg_obj) in enumerate(self.arg_objs):
            if arg_obj is None:
                return None
            arg_doc, _ = arg_obj.get_hover(include_doc=False)
            if i == change_arg:
                i0 = arg_doc.lower().find(change_strings[0].lower())
                if i0 >= 0:
                    i1 = i0 + len(change_strings[0])
                    arg_doc = arg_doc[:i0] + change_strings[1] + arg_doc[i1:]
            interface_array.append("{0} :: {1}".format(arg_doc, arg_obj.name))
        name = self.name
        if name_replace is not None:
            name = name_replace
        interface_array.append("END SUBROUTINE {0}".format(name))
        return "\n".join(interface_array)

    def check_valid_parent(self):
        if self.parent is not None:
            parent_type = self.parent.get_type()
            if (parent_type == CLASS_TYPE_ID) or (parent_type >= BLOCK_TYPE_ID):
                return False
        return True

    def get_diagnostics(self):
        errors = []
        for missing_obj in self.missing_args:
            new_diag = fortran_diagnostic(
                missing_obj.sline-1,
                'Variable "{0}" with INTENT keyword not found in argument list'.format(missing_obj.name),
                severity=1, find_word=missing_obj.name
            )
            errors.append(new_diag)
        implicit_flag = self.get_implicit()
        if (implicit_flag is None) or (implicit_flag):
            return errors
        arg_list = self.args.replace(' ', '').split(',')
        for (i, arg_obj) in enumerate(self.arg_objs):
            if arg_obj is None:
                arg_name = arg_list[i].strip()
                new_diag = fortran_diagnostic(
                    self.sline-1, 'No matching declaration found for argument "{0}"'.format(arg_name),
                    severity=1, find_word=arg_name
                )
                errors.append(new_diag)
        return errors


class fortran_function(fortran_subroutine):
    def __init__(self, file_obj, line_number, name, args="",
                 mod_fun=False, keywords=[], return_type=None, result_var=None):
        self.base_setup(file_obj, line_number, name, keywords=keywords)
        self.args = args.replace(' ', '').lower()
        self.args_snip = self.args
        self.arg_objs = []
        self.in_children = []
        self.missing_args = []
        self.mod_scope = mod_fun
        self.result_var = result_var
        self.result_obj = None
        if return_type is not None:
            self.return_type = return_type[0]
        else:
            self.return_type = None

    def copy_interface(self, copy_source):
        # Copy arguments and returns
        self.args = copy_source.args
        self.args_snip = copy_source.args_snip
        self.arg_objs = copy_source.arg_objs
        self.result_var = copy_source.result_var
        self.result_obj = copy_source.result_obj
        # Get current fields
        child_names = []
        for child in self.children:
            child_names.append(child.name.lower())
        # Import arg_objs from copy object
        self.in_children = []
        for child in copy_source.arg_objs:
            if child is None:
                continue
            if child.name.lower() not in child_names:
                self.in_children.append(child)
        if copy_source.result_obj is not None:
            if copy_source.result_obj.name.lower() not in child_names:
                self.in_children.append(copy_source.result_obj)

    def resolve_link(self, obj_tree):
        self.resolve_arg_link(obj_tree)
        if self.result_var is not None:
            result_var_lower = self.result_var.lower()
            for child in self.children:
                if child.name.lower() == result_var_lower:
                    self.result_obj = child

    def get_type(self):
        return FUNCTION_TYPE_ID

    def get_desc(self):
        if self.result_obj is not None:
            return self.result_obj.get_desc() + ' FUNCTION'
        if self.return_type is not None:
            return self.return_type + ' FUNCTION'
        return 'FUNCTION'

    def is_callable(self):
        return False

    def get_hover(self, long=False, include_doc=True, drop_arg=-1):
        fun_sig, _ = self.get_snippet(drop_arg=drop_arg)
        fun_return = ""
        if self.result_obj is not None:
            fun_return, _ = self.result_obj.get_hover(include_doc=False)
        if self.return_type is not None:
            fun_return = self.return_type
        keyword_list = get_keywords(self.keywords)
        keyword_list.append("FUNCTION")
        hover_array = ["{0} {1} {2}".format(fun_return, " ".join(keyword_list), fun_sig)]
        doc_str = self.get_documentation()
        if include_doc and (doc_str is not None):
            hover_array[0] += "\n" + doc_str
        if long:
            for (i, arg_obj) in enumerate(self.arg_objs):
                if (arg_obj is None) or (i == drop_arg):
                    continue
                arg_doc, _ = arg_obj.get_hover(include_doc=False)
                hover_array.append("{0} :: {1}".format(arg_doc, arg_obj.name))
                doc_str = arg_obj.get_documentation()
                if include_doc and (doc_str is not None):
                    hover_array += doc_str.splitlines()
        return "\n ".join(hover_array), long

    def get_interface(self, name_replace=None, change_arg=-1, change_strings=None):
        fun_sig, _ = self.get_snippet(name_replace=name_replace)
        keyword_list = []
        if self.return_type is not None:
            keyword_list.append(self.return_type)
        if self.result_obj is not None:
            fun_sig += " RESULT({0})".format(self.result_obj.name)
        keyword_list += get_keywords(self.keywords)
        keyword_list.append("FUNCTION ")
        interface_array = [" ".join(keyword_list) + fun_sig]
        for (i, arg_obj) in enumerate(self.arg_objs):
            if arg_obj is None:
                return None
            arg_doc, _ = arg_obj.get_hover(include_doc=False)
            if i == change_arg:
                i0 = arg_doc.lower().find(change_strings[0].lower())
                if i0 >= 0:
                    i1 = i0 + len(change_strings[0])
                    arg_doc = arg_doc[:i0] + change_strings[1] + arg_doc[i1:]
            interface_array.append("{0} :: {1}".format(arg_doc, arg_obj.name))
        if self.result_obj is not None:
            arg_doc, _ = self.result_obj.get_hover(include_doc=False)
            interface_array.append("{0} :: {1}".format(arg_doc, self.result_obj.name))
        name = self.name
        if name_replace is not None:
            name = name_replace
        interface_array.append("END FUNCTION {0}".format(name))
        return "\n".join(interface_array)


class fortran_type(fortran_scope):
    def __init__(self, file_obj, line_number, name, keywords):
        self.base_setup(file_obj, line_number, name, keywords=keywords)
        #
        self.in_children = []
        self.inherit = None
        self.inherit_var = None
        if self.keywords.count(KEYWORD_ID_DICT['public']) > 0:
            self.vis = 1
        if self.keywords.count(KEYWORD_ID_DICT['private']) > 0:
            self.vis = -1

    def get_type(self):
        return CLASS_TYPE_ID

    def get_desc(self):
        return 'TYPE'

    def get_children(self, public_only=False):
        tmp_list = copy.copy(self.children)
        tmp_list.extend(self.in_children)
        return tmp_list

    def resolve_inherit(self, obj_tree):
        if self.inherit is None:
            return
        #
        self.inherit_var = find_in_scope(self.parent, self.inherit, obj_tree)
        if self.inherit_var is not None:
            self.inherit_var.resolve_inherit(obj_tree)
            # Get current fields
            child_names = []
            for child in self.children:
                child_names.append(child.name.lower())
                child.resolve_inherit(obj_tree)
            # Import for parent objects
            self.in_children = []
            for child in self.inherit_var.get_children():
                if child.name.lower() not in child_names:
                    self.in_children.append(child)

    def get_overriden(self, field_name):
        ret_list = []
        field_name = field_name.lower()
        for child in self.children:
            if field_name == child.name.lower():
                ret_list.append(child)
                break
        if self.inherit_var is not None:
            ret_list += self.inherit_var.get_overriden(field_name)
        return ret_list

    def check_valid_parent(self):
        if self.parent is None:
            return False
        else:
            parent_type = self.parent.get_type()
            if (parent_type == CLASS_TYPE_ID) or (parent_type >= BLOCK_TYPE_ID):
                return False
        return True

    def get_diagnostics(self):
        errors = []
        for in_child in self.in_children:
            if in_child.keywords.count(KEYWORD_ID_DICT['deferred']) > 0:
                new_diag = fortran_diagnostic(
                    self.eline - 1, 'Deferred procedure "{0}" not implemented'.format(in_child.name),
                    severity=1
                )
                new_diag.add_related(path=in_child.file.path, line=in_child.sline-1,
                                     message='Inherited procedure declaration')
                errors.append(new_diag)
        return errors

    def get_actions(self, sline, eline):
        actions = []
        edits = []
        line_number = self.eline - 1
        if (line_number < sline) or (line_number > eline):
            return actions
        if self.contains_start is None:
            edits.append({
                "range": {
                    "start": {"line": line_number, "character": 0},
                    "end": {"line": line_number, "character": 0}
                },
                "newText": "CONTAINS\n"
            })
        #
        diagnostics = []
        has_edits = False
        file_uri = path_to_uri(self.file.path)
        for in_child in self.in_children:
            if in_child.keywords.count(KEYWORD_ID_DICT['deferred']) > 0:
                # Get interface
                interface_string = in_child.get_interface(
                    name_replace=in_child.name,
                    change_strings=('class({0})'.format(in_child.parent.name), 'CLASS({0})'.format(self.name))
                )
                if interface_string is None:
                    continue
                interface_path, interface_edits = self.parent.add_subroutine(interface_string, no_contains=has_edits)
                if interface_path != self.file.path:
                    continue
                edits.append({
                    "range": {
                        "start": {"line": line_number, "character": 0},
                        "end": {"line": line_number, "character": 0}
                    },
                    "newText": "  PROCEDURE :: {0} => {0}\n".format(in_child.name)
                })
                edits += interface_edits
                new_diag = fortran_diagnostic(
                    line_number, 'Deferred procedure "{0}" not implemented'.format(in_child.name),
                    severity=1
                )
                new_diag.add_related(path=in_child.file.path, line=in_child.sline-1,
                                     message='Inherited procedure declaration')
                diagnostics.append(new_diag)
                has_edits = True
        #
        if has_edits:
            actions = [{
                "title": "Implement deferred procedures",
                "kind": "quickfix",
                "edit": {
                    "changes": {
                        file_uri: edits
                    }
                },
                "diagnostics": diagnostics
            }]
        return actions


class fortran_block(fortran_scope):
    def __init__(self, file_obj, line_number, name):
        self.base_setup(file_obj, line_number, name)

    def get_type(self):
        return BLOCK_TYPE_ID

    def get_desc(self):
        return 'BLOCK'

    def get_children(self, public_only=False):
        return self.children

    def req_named_end(self):
        return True


class fortran_do(fortran_block):
    def __init__(self, file_obj, line_number, name):
        self.base_setup(file_obj, line_number, name)

    def get_type(self):
        return DO_TYPE_ID

    def get_desc(self):
        return 'DO'


class fortran_where(fortran_block):
    def __init__(self, file_obj, line_number, name):
        self.base_setup(file_obj, line_number, name)

    def get_type(self):
        return WHERE_TYPE_ID

    def get_desc(self):
        return 'WHERE'


class fortran_if(fortran_block):
    def __init__(self, file_obj, line_number, name):
        self.base_setup(file_obj, line_number, name)

    def get_type(self):
        return IF_TYPE_ID

    def get_desc(self):
        return 'IF'


class fortran_associate(fortran_block):
    def __init__(self, file_obj, line_number, name):
        self.base_setup(file_obj, line_number, name)

    def get_type(self):
        return ASSOC_TYPE_ID

    def get_desc(self):
        return 'ASSOCIATE'


class fortran_enum(fortran_block):
    def __init__(self, file_obj, line_number, name):
        self.base_setup(file_obj, line_number, name)

    def get_type(self):
        return ENUM_TYPE_ID

    def get_desc(self):
        return 'ENUM'


class fortran_select(fortran_block):
    def __init__(self, file_obj, line_number, name, select_info):
        self.base_setup(file_obj, line_number, name)
        self.select_type = select_info[0]
        self.binding_name = None
        self.bound_var = None
        self.binding_type = None
        if self.select_type == 2:
            binding_split = select_info[1].split('=>')
            if len(binding_split) == 1:
                self.bound_var = binding_split[0].strip()
            elif len(binding_split) == 2:
                self.binding_name = binding_split[0].strip()
                self.bound_var = binding_split[1].strip()
        elif self.select_type == 3:
            self.binding_type = select_info[1]
        # Close previous "TYPE IS" region if open
        if (file_obj.current_scope is not None) \
           and (file_obj.current_scope.get_type() == SELECT_TYPE_ID)\
           and file_obj.current_scope.is_type_region():
            file_obj.end_scope(line_number)

    def get_type(self):
        return SELECT_TYPE_ID

    def get_desc(self):
        return 'SELECT'

    def is_type_binding(self):
        return (self.select_type == 2)

    def is_type_region(self):
        return ((self.select_type == 3) or (self.select_type == 4))

    def create_binding_variable(self, file_obj, line_number, var_desc, case_type):
        if self.parent.get_type() != SELECT_TYPE_ID:
            return None
        binding_name = None
        bound_var = None
        if (self.parent is not None) and self.parent.is_type_binding():
            binding_name = self.parent.binding_name
            bound_var = self.parent.bound_var
        # Check for default case
        if (binding_name is not None) and (case_type != 4):
            bound_var = None
        # Create variable
        if binding_name is not None:
            return fortran_var(file_obj, line_number, binding_name, var_desc, [], link_obj=bound_var)
        elif (binding_name is None) and (bound_var is not None):
            return fortran_var(file_obj, line_number, bound_var, var_desc, [])
        return None


class fortran_int(fortran_scope):
    def __init__(self, file_obj, line_number, name, abstract=False):
        self.base_setup(file_obj, line_number, name)
        self.mems = []
        self.abstract = abstract
        self.external = name.startswith('#GEN_INT') and (not abstract)

    def get_type(self):
        return INTERFACE_TYPE_ID

    def get_desc(self):
        return 'INTERFACE'

    def is_callable(self):
        return True

    def is_external_int(self):
        return self.external

    def is_abstract(self):
        return self.abstract

    def resolve_link(self, obj_tree):
        if self.parent is None:
            return
        self.mems = []
        for member in self.members:
            mem_obj = find_in_scope(self.parent, member, obj_tree)
            if mem_obj is not None:
                self.mems.append(mem_obj)
        for child in self.children:
            child.resolve_link(obj_tree)


class fortran_var(fortran_obj):
    def __init__(self, file_obj, line_number, name, var_desc, keywords,
                 keyword_info={}, link_obj=None):
        self.base_setup(file_obj, line_number, name, var_desc, keywords,
                        keyword_info, link_obj)

    def base_setup(self, file_obj, line_number, name, var_desc, keywords,
                   keyword_info, link_obj):
        self.file = file_obj
        self.sline = line_number
        self.eline = line_number
        self.name = name
        self.desc = var_desc
        self.keywords = keywords
        self.keyword_info = keyword_info
        self.doc_str = None
        self.callable = (CLASS_VAR_REGEX.match(var_desc) is not None)
        self.children = []
        self.use = []
        self.vis = 0
        self.parent = None
        self.link_obj = None
        if link_obj is not None:
            self.link_name = link_obj.lower()
        else:
            self.link_name = None
        if file_obj.enc_scope_name is not None:
            self.FQSN = file_obj.enc_scope_name.lower() + "::" + self.name.lower()
        else:
            self.FQSN = self.name.lower()
        if self.keywords.count(KEYWORD_ID_DICT['public']) > 0:
            self.vis = 1
        if self.keywords.count(KEYWORD_ID_DICT['private']) > 0:
            self.vis = -1

    def update_fqsn(self, enc_scope=None):
        if enc_scope is not None:
            self.FQSN = enc_scope.lower() + "::" + self.name.lower()
        else:
            self.FQSN = self.name.lower()
        for child in self.children:
            child.update_fqsn(self.FQSN)

    def resolve_link(self, obj_tree):
        if self.link_name is None:
            return
        if self.parent is not None:
            link_obj = find_in_scope(self.parent, self.link_name, obj_tree)
            if link_obj is not None:
                self.link_obj = link_obj

    def get_type(self):
        if self.link_obj is not None:
            return self.link_obj.get_type()
        # Normal variable
        return VAR_TYPE_ID

    def get_desc(self):
        if self.link_obj is not None:
            return self.link_obj.get_desc()
        # Normal variable
        return self.desc

    def set_dim(self, dim_str):
        if KEYWORD_ID_DICT['dimension'] not in self.keywords:
            self.keywords.append(KEYWORD_ID_DICT['dimension'])
            self.keyword_info['dimension'] = dim_str
            self.keywords.sort()

    def get_snippet(self, name_replace=None, drop_arg=-1):
        name = self.name
        if name_replace is not None:
            name = name_replace
        if self.link_obj is not None:
            return self.link_obj.get_snippet(name, drop_arg)
        # Normal variable
        return None, None

    def get_hover(self, long=False, include_doc=True, drop_arg=-1):
        doc_str = self.get_documentation()
        hover_str = ", ".join([self.desc] + get_keywords(self.keywords, self.keyword_info))
        if include_doc and (doc_str is not None):
            hover_str += "\n {0}".format('\n '.join(doc_str.splitlines()))
        return hover_str, True

    def is_optional(self):
        if self.keywords.count(KEYWORD_ID_DICT['optional']) > 0:
            return True
        else:
            return False

    def is_callable(self):
        return self.callable

    def check_definition(self, obj_tree, known_types={}, interface=False):
        # Check for type definition in scope
        type_match = DEF_KIND_REGEX.match(self.desc)
        if type_match is not None:
            var_type = type_match.group(1).strip().lower()
            if var_type == 'procedure':
                return None, known_types
            desc_obj_name = type_match.group(2).strip().lower()
            if desc_obj_name not in known_types:
                type_def = find_in_scope(self.parent, desc_obj_name, obj_tree, interface=interface)
                if type_def is None:
                    type_defs = find_in_workspace(obj_tree, desc_obj_name, filter_public=True, exact_match=True)
                    known_types[desc_obj_name] = None
                    var_type = type_match.group(1).strip().lower()
                    filter_id = VAR_TYPE_ID
                    if (var_type == 'class') or (var_type == 'type'):
                        filter_id = CLASS_TYPE_ID
                    for type_def in type_defs:
                        if type_def.get_type() == filter_id:
                            known_types[desc_obj_name] = (1, type_def)
                            break
                else:
                    known_types[desc_obj_name] = (0, type_def)
            type_info = known_types[desc_obj_name]
            if type_info is not None:
                if type_info[0] == 1:
                    if interface:
                        out_diag = fortran_diagnostic(
                            self.sline-1, message='Object "{0}" not found in scope'.format(desc_obj_name),
                            severity=1, find_word=desc_obj_name
                        )
                        type_def = type_info[1]
                        out_diag.add_related(path=type_def.file.path, line=type_def.sline-1, message='Possible object')
                    else:
                        out_diag = fortran_diagnostic(
                            self.sline-1, message='Object "{0}" not imported in interface'.format(desc_obj_name),
                            severity=1, find_word=desc_obj_name
                        )
                    return out_diag, known_types
        return None, known_types


class fortran_meth(fortran_var):
    def __init__(self, file_obj, line_number, name, var_desc, keywords,
                 keyword_info, link_obj=None):
        self.base_setup(file_obj, line_number, name, var_desc, keywords,
                        keyword_info, link_obj)
        self.drop_arg = -1
        self.pass_name = keyword_info.get('pass')
        if link_obj is None:
            self.link_name = get_paren_substring(var_desc.lower())

    def set_parent(self, parent_obj):
        self.parent = parent_obj
        if self.parent.get_type() == CLASS_TYPE_ID:
            if self.keywords.count(KEYWORD_ID_DICT['nopass']) == 0:
                self.drop_arg = 0
            if (self.parent.contains_start is not None) and \
               (self.sline > self.parent.contains_start) and (self.link_name is None):
                self.link_name = self.name.lower()

    def get_snippet(self, name_replace=None, drop_arg=-1):
        if self.link_obj is not None:
            if name_replace is None:
                name = self.name
            else:
                name = name_replace
            return self.link_obj.get_snippet(name, self.drop_arg)
        return None, None

    def get_type(self):
        if self.link_obj is not None:
            return self.link_obj.get_type()
        # Generic
        return METH_TYPE_ID

    def get_documentation(self):
        if (self.link_obj is not None) and (self.doc_str is None):
            return self.link_obj.get_documentation()
        return self.doc_str

    def get_hover(self, long=False, include_doc=True, drop_arg=-1):
        doc_str = self.get_documentation()
        if long:
            if self.link_obj is None:
                sub_sig, _ = self.get_snippet()
                hover_str = "{0} {1}".format(self.get_desc(), sub_sig)
                if include_doc and (doc_str is not None):
                    hover_str += "\n{0}".format(doc_str)
            else:
                link_hover, _ = self.link_obj.get_hover(long=True, include_doc=include_doc, drop_arg=self.drop_arg)
                hover_split = link_hover.splitlines()
                call_sig = hover_split[0]
                paren_start = call_sig.rfind('(')
                link_name_len = len(self.link_obj.name)
                call_sig = call_sig[:paren_start-link_name_len] + self.name + call_sig[paren_start:]
                hover_split = hover_split[1:]
                if include_doc and (self.doc_str is not None):
                    # Replace linked docs with current object's docs
                    if (len(hover_split) > 0) and (hover_split[0].count('!!') > 0):
                        for (i, hover_line) in enumerate(hover_split):
                            if hover_line.count('!!') == 0:
                                hover_split = hover_split[i:]
                                break
                        else:  # All lines are docs
                            hover_split = []
                    hover_split = [self.doc_str] + hover_split
                hover_str = '\n'.join([call_sig] + hover_split)
            return hover_str, True
        else:
            hover_str = ", ".join([self.desc] + get_keywords(self.keywords))
            if include_doc and (doc_str is not None):
                hover_str += "\n{0}".format(doc_str)
            return hover_str, True

    def get_signature(self, drop_arg=-1):
        if self.link_obj is not None:
            call_sig, _ = self.get_snippet()
            _, _, arg_sigs = self.link_obj.get_signature(self.drop_arg)
            return call_sig, self.get_documentation(), arg_sigs
        return None, None, None

    def get_interface(self, name_replace=None, change_arg=-1, change_strings=None):
        if self.link_obj is not None:
            return self.link_obj.get_interface(name_replace, self.drop_arg, change_strings)
        return None

    def resolve_link(self, obj_tree):
        if self.link_name is None:
            return
        if self.parent is not None:
            if self.parent.get_type() == CLASS_TYPE_ID:
                link_obj = find_in_scope(self.parent.parent, self.link_name, obj_tree)
            else:
                link_obj = find_in_scope(self.parent, self.link_name, obj_tree)
            if link_obj is not None:
                self.link_obj = link_obj
                if self.pass_name is not None:
                    self.pass_name = self.pass_name.lower()
                    for i, arg in enumerate(link_obj.args_snip.split(',')):
                        if arg.lower() == self.pass_name:
                            self.drop_arg = i
                            break

    def is_callable(self):
        return True

    def check_definition(self, obj_tree, known_types={}, interface=False):
        return None, known_types


class fortran_ast:
    def __init__(self, file_obj=None):
        self.file = file_obj
        self.path = None
        if file_obj is not None:
            self.path = file_obj.path
        self.global_dict = {}
        self.scope_list = []
        self.variable_list = []
        self.public_list = []
        self.private_list = []
        self.scope_stack = []
        self.end_stack = []
        self.pp_if = []
        self.include_stmnts = []
        self.end_errors = []
        self.parse_errors = []
        self.none_scope = None
        self.inc_scope = None
        self.current_scope = None
        self.END_SCOPE_WORD = None
        self.enc_scope_name = None
        self.last_obj = None
        self.pending_doc = None

    def create_none_scope(self):
        """Create empty scope to hold non-module contained items"""
        if self.none_scope is not None:
            raise ValueError
        self.none_scope = fortran_program(self, 1, "main")
        self.add_scope(self.none_scope, re.compile(r'[ ]*END[ ]*PROGRAM', re.I), exportable=False)

    def get_enc_scope_name(self):
        """Get current enclosing scope name"""
        if self.current_scope is None:
            return None
        return self.current_scope.FQSN

    def add_scope(self, new_scope, END_SCOPE_WORD, exportable=True, req_container=False):
        self.scope_list.append(new_scope)
        if self.current_scope is None:
            if req_container:
                self.create_none_scope()
                new_scope.FQSN = self.none_scope.FQSN + "::" + new_scope.name.lower()
                self.current_scope.add_child(new_scope)
                self.scope_stack.append(self.current_scope)
            else:
                if exportable:
                    self.global_dict[new_scope.FQSN] = new_scope
        else:
            self.current_scope.add_child(new_scope)
            self.scope_stack.append(self.current_scope)
        if self.END_SCOPE_WORD is not None:
            self.end_stack.append(self.END_SCOPE_WORD)
        self.current_scope = new_scope
        self.END_SCOPE_WORD = END_SCOPE_WORD
        self.enc_scope_name = self.get_enc_scope_name()
        self.last_obj = new_scope
        if self.pending_doc is not None:
            self.last_obj.add_doc(self.pending_doc)
            self.pending_doc = None

    def end_scope(self, line_number, check=True):
        if ((self.current_scope is None) or (self.current_scope is self.none_scope)) and check:
            self.end_errors.append([-1, line_number])
            return
        self.current_scope.end(line_number)
        if len(self.scope_stack) > 0:
            self.current_scope = self.scope_stack.pop()
        else:
            self.current_scope = None
        if len(self.end_stack) > 0:
            self.END_SCOPE_WORD = self.end_stack.pop()
        else:
            self.END_SCOPE_WORD = None
        self.enc_scope_name = self.get_enc_scope_name()

    def add_variable(self, new_var):
        if self.current_scope is None:
            self.create_none_scope()
            new_var.FQSN = self.none_scope.FQSN + "::" + new_var.name.lower()
        self.current_scope.add_child(new_var)
        self.variable_list.append(new_var)
        self.last_obj = new_var
        if self.pending_doc is not None:
            self.last_obj.add_doc(self.pending_doc)
            self.pending_doc = None

    def add_int_member(self, key):
        self.current_scope.add_member(key)

    def add_private(self, name):
        self.private_list.append(self.enc_scope_name+'::'+name)

    def add_public(self, name):
        self.public_list.append(self.enc_scope_name+'::'+name)

    def add_use(self, mod_word, line_number, only_list):
        if self.current_scope is None:
            self.create_none_scope()
        self.current_scope.add_use(mod_word, line_number, only_list)

    def add_include(self, path, line_number):
        self.include_stmnts.append([line_number, path, []])

    def add_doc(self, doc_string, forward=False):
        if doc_string == '':
            return
        if forward:
            self.pending_doc = doc_string
        else:
            if self.last_obj is not None:
                self.last_obj.add_doc(doc_string)

    def start_ppif(self, line_number):
        self.pp_if.append([line_number-1, -1])

    def end_ppif(self, line_number):
        if len(self.pp_if) > 0:
            self.pp_if[-1][1] = line_number-1

    def get_scopes(self, line_number=None):
        if line_number is None:
            return self.scope_list
        scope_list = []
        for scope in self.scope_list:
            if (line_number >= scope.sline) and (line_number <= scope.eline):
                scope_list.append(scope)
                for ancestor in scope.get_ancestors():
                    scope_list.append(ancestor)
        if (len(scope_list) == 0) and (self.none_scope is not None):
            return [self.none_scope]
        return scope_list

    def get_inner_scope(self, line_number):
        scope_sline = -1
        curr_scope = None
        for scope in self.scope_list:
            if scope.sline > scope_sline:
                if (line_number >= scope.sline) and (line_number <= scope.eline):
                    curr_scope = scope
                    scope_sline = scope.sline
        if (curr_scope is None) and (self.none_scope is not None):
            return self.none_scope
        return curr_scope

    def get_object(self, FQSN):
        FQSN_split = FQSN.split("::")
        curr_obj = self.global_dict.get(FQSN_split[0])
        if curr_obj is None:
            # Look for non-exportable scopes
            for scope in self.scope_list:
                if FQSN_split[0] == scope.FQSN:
                    curr_obj = scope
                    break
        if curr_obj is None:
            return None
        if len(FQSN_split) > 1:
            for name in FQSN_split[1:]:
                next_obj = None
                for child in curr_obj.children:
                    if child.name.startswith("#GEN_INT"):
                        for int_child in child.get_children():
                            if int_child.name == name:
                                next_obj = int_child
                                break
                        if next_obj is not None:
                            break
                    if child.name == name:
                        next_obj = child
                        break
                if next_obj is None:
                    return None
                curr_obj = next_obj
        return curr_obj

    def resolve_includes(self, workspace, path=None):
        file_dir = os.path.dirname(self.file.path)
        for include_path in self.include_stmnts:
            file_path = os.path.normpath(os.path.join(file_dir, include_path[1]))
            if path is not None:
                if not (path == file_path):
                    continue
            parent_scope = self.get_inner_scope(include_path[0])
            added_entities = include_path[2]
            if file_path in workspace:
                include_file = workspace[file_path]
                include_ast = include_file.ast
                if include_ast.none_scope is not None:
                    if include_ast.inc_scope is None:
                        include_ast.inc_scope = include_ast.none_scope
                    # Remove old objects
                    for obj in added_entities:
                        parent_scope.children.remove(obj)
                    added_entities = []
                    for child in include_ast.inc_scope.children:
                        added_entities.append(child)
                        parent_scope.add_child(child)
                        child.update_fqsn(parent_scope.FQSN)
                    include_ast.none_scope = parent_scope
                    include_path[2] = added_entities

    def close_file(self, line_number):
        # Close open scopes
        while self.current_scope is not None:
            self.end_scope(line_number, check=False)
        # Close and delist none_scope
        if self.none_scope is not None:
            self.none_scope.end(line_number)
            self.scope_list.remove(self.none_scope)
        # Tasks to be done when file parsing is finished
        for private_name in self.private_list:
            obj = self.get_object(private_name)
            if obj is not None:
                obj.set_visibility(-1)
        for public_name in self.public_list:
            obj = self.get_object(public_name)
            if obj is not None:
                obj.set_visibility(1)

    def check_file(self, obj_tree):
        errors = []
        tmp_list = self.scope_list[:]
        if self.none_scope is not None:
            tmp_list += [self.none_scope]
        for error in self.parse_errors:
            errors.append({
                "range": {
                    "start": {"line": error["line"]-1, "character": error["schar"]},
                    "end": {"line": error["line"]-1, "character": error["echar"]}
                },
                "message": error["mess"],
                "severity": error["sev"]
            })
        for error in self.end_errors:
            if error[0] >= 0:
                message = 'Unexpected end of scope at line {0}'.format(error[0])
            else:
                message = 'Unexpected end statement: No open scopes'
            errors.append(build_diagnostic(error[1]-1, message=message, severity=1))
        for scope in tmp_list:
            if not scope.check_valid_parent():
                errors.append(build_diagnostic(
                    scope.sline-1, message='Invalid parent for "{0}" declaration'.format(scope.get_desc()),
                    severity=1
                ))
            errors += scope.check_use(obj_tree)
            errors += scope.check_definitions(obj_tree)
            errors += scope.get_diagnostics()
        diagnostics = []
        for error in errors:
            diagnostics.append(error.build(self.file))
        return diagnostics
