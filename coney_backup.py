#!/usr/bin/python3
# MIT License
#
# Copyright (c) 2021 Lukas Cone
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import yaml
import difflib
import subprocess
import argparse
import os.path as path
from os import rename, remove

config_version = 1
app_version = '1.0.0'

print('Coney Backup v' + app_version)

parser = argparse.ArgumentParser()
parser.add_argument('--unit-test', action='store_true',
                    help='handle config as unit test')
parser.add_argument('--verbose', '-v', action='store_true')
parser.add_argument('-j', '--job', help='run only specified job')
parser.add_argument('config_path', help='path to a yaml config')
args = parser.parse_args()


def load_schema(loaded_schema):
    if not 'version' in loaded_schema:
        raise RuntimeError('Missing version key!')

    schema_version = loaded_schema['version']

    if config_version < schema_version:
        raise RuntimeError('Version mismatch. Schema version: ' +
                           str(schema_version) + '. Expected <= ' + str(config_version))
    return loaded_schema


def load_schema_from_file(file_name):
    loaded_schema = yaml.safe_load(open(file_name, 'r'))
    load_schema(loaded_schema)


def traceback_noarg(parent_key, func):
    try:
        func()
    except Exception as e:
        raise RuntimeError(str(e) + '\n\tTraceback: ' + str(parent_key))


def traceback(parent_key, func, args):
    try:
        func(args)
    except Exception as e:
        raise RuntimeError(str(e) + '\n\tTraceback: ' + str(parent_key))


def build_cmd_include_file(wildcard):
    return ' -ir!{}'.format(wildcard)


def build_cmd_exclude_file(wildcard):
    return ' -xr!{}'.format(wildcard)


def collect_includes(schema):
    retval = []
    jobs = schema['jobs']
    for i in range(len(jobs)):
        cur_job = next((sub for sub in jobs.values() if sub['id'] == i), {})
        if 'include' in cur_job:
            inc = cur_job['include']
            if type(inc) != list:
                retval.append(inc)
            else:
                retval.extend(inc)
    return retval


def check_inc_collisions(items):
    num_items = len(items)
    colided = False

    for i in range(num_items):
        for t in range(i + 1, num_items):
            if items[i] == items[t]:
                print("Error colliding include pattern {}".format(items[i]))
                colided = True
    if colided:
        raise RuntimeError("There were some include pattern collisions!")


def build_cmd_files(schema, job):
    num_jobs = len(schema['jobs'])
    is_include = 'include' in job
    is_exclude = 'exclude' in job
    all_includes = schema['all_includes']

    if is_include and is_exclude:
        print("Job contains both include and exclude. Ignoring exclude!")
        is_exclude = False
    if is_include:
        include_wcs = job['include']
        if type(include_wcs) != list:
            return build_cmd_include_file(include_wcs)
        else:
            retval = ''
            for i in include_wcs:
                retval = retval + build_cmd_include_file(i)
            return retval
    if is_exclude:
        retval = ''
        exclude_wcs = job['exclude'] + all_includes
        for i in exclude_wcs:
            retval = retval + build_cmd_exclude_file(i)
        return retval

    if num_jobs > 1 and all_includes and num_jobs == job['id'] + 1:
        retval = ''
        for i in all_includes:
            retval = retval + build_cmd_exclude_file(i)
        return retval

    return ' *'


def pick_dval(name, base, override):
    if name in override:
        retval = override[name]
        return retval if retval else ''
    if name in base:
        retval = base[name]
        return retval if retval else ''
    return ''


def build_cmd_archive(arc, override):
    arc_name = pick_dval('out_dir', arc, override)

    if arc_name and not arc_name.endswith('/'):
        arc_name = arc_name + '/'

    arc_name = arc_name + arc['name']

    if 'name' in override:
        arc_name = arc_name + '.' + override['name']

    extension = pick_dval('extension', arc, override)

    if not extension:
        arc_type = pick_dval('type', arc, override)
        extension = (arc_type if arc_type else '7z')

    patch_name = arc_name + '.patch.' + extension
    arc_name = arc_name + '.' + extension

    return (arc_name, patch_name)


def build_cmd_pwd(arc, override):
    pwd = pick_dval('password', arc, override)

    return ' -p{}'.format(pwd) if pwd else ''


COMP_LEVELS = {
    'store': 0,
    'fastest': 1,
    'fast': 3,
    'normal': 5,
    'maximum': 7,
    'ultra': 9,
}


def build_cmd_clevel(arc, override):
    level = pick_dval('compression_level', arc, override)

    if not level:
        return ''

    if not level in COMP_LEVELS:
        raise RuntimeError("Undefined compression level: {}!".format(level))
    return ' -mx={}'.format(COMP_LEVELS[level])


ZIP_COMP_TYPES = [
    'copy',
    'deflate',
    'deflate64',
    'bzip2',
    'lzma',
    'ppmd'
]

ZIP_ENCRYPT_TYPE = [
    'none',
    'zipcrypto',
    'aes128',
    'aes192',
    'aes256',
]


def build_zip_params(params):
    retval = ''
    if 'compression' in params:
        comp = params['compression']
        comp_type = None
        deflate64 = False
        if 'type' in comp:
            comp_type = comp['type']
            if not comp_type in ZIP_COMP_TYPES:
                raise RuntimeError(
                    "Undefined zip compression type: {}!".format(comp_type))
            retval = retval + ' -mm={}'.format(comp_type)
        if comp_type == 'deflate64':
            deflate64 = True
            comp_type = 'deflate'
        if comp_type and comp_type in comp:
            comp_data = comp[comp_type]
            if comp_type.startswith('deflate'):
                if 'num_fast_bytes' in comp_data:
                    fast_bytes = comp_data['num_fast_bytes']
                    if deflate64 and fast_bytes < 3 or fast_bytes > 257:
                        raise RuntimeError(
                            "Deflate param num_fast_bytes out of range: {} expected [3 - 257]!".format(fast_bytes))
                    elif fast_bytes < 3 or fast_bytes > 258:
                        raise RuntimeError(
                            "Deflate param num_fast_bytes out of range: {} expected [3 - 258]!".format(fast_bytes))
                    retval = retval + ' -mfb={}'.format(fast_bytes)
                if 'num_passes' in comp_data:
                    num_passes = comp_data['num_passes']
                    if num_passes < 1 or num_passes > 15:
                        raise RuntimeError(
                            "Deflate param num_passes out of range: {} expected [1 - 15]!".format(num_passes))
                    retval = retval + ' -mpass={}'.format(num_passes)
            elif comp_type == 'bzip2':
                if 'num_passes' in comp_data:
                    num_passes = comp_data['num_passes']
                    if num_passes < 1 or num_passes > 10:
                        raise RuntimeError(
                            "Deflate param num_passes out of range: {} expected [1 - 10]!".format(num_passes))
                    retval = retval + ' -mpass={}'.format(num_passes)
                if 'dict_size' in comp_data:
                    dict_size = comp_data['dict_size']
                    retval = retval + ' -md={}'.format(dict_size)
            elif comp_type == 'ppmd':
                if 'mem_usage' in comp_data:
                    mem_usage = comp_data['mem_usage']
                    retval = retval + ' -mmem={}'.format(mem_usage)
                if 'model_order' in comp_data:
                    model_order = comp_data['model_order']
                    if model_order < 2 or model_order > 16:
                        raise RuntimeError(
                            "PPMD param model_order out of range: {} expected [2 - 16]!".format(model_order))
                    retval = retval + ' -mo={}'.format(model_order)
    if 'encryption' in params:
        encryption = params['encryption']
        if encryption != 'none':
            if not encryption in ZIP_ENCRYPT_TYPE:
                raise RuntimeError(
                    "Undefined encryption type: {}!".format(encryption))
            retval = retval + ' -mem={}'.format(encryption)
    if 'metadata' in params and not params['metadata']:
        retval = retval + ' -mtc=off'
    if 'unicode' in params and params['unicode']:
        retval = retval + ' -mcu=on'
    return retval


MATCH_FINDERS = [
    'bt2',
    'bt3',
    'bt4',
    'hc4',
]


def build_7z_lzma_params(item):
    retval = ''
    if 'fast_mode' in item and item['fast_mode']:
        retval = retval + ':a=0'
    if 'dict_size' in item:
        retval = retval + ':d=' + item['dict_size']
    if 'match_finder' in item:
        match_finder = item['match_finder']
        if not match_finder in MATCH_FINDERS:
            raise RuntimeError(
                "Undefined match_finder type: {}!".format(match_finder))
        retval = retval + ':mf=' + match_finder
    if 'num_fast_bytes' in item:
        num_fast_bytes = item['num_fast_bytes']
        if num_fast_bytes < 5 or num_fast_bytes > 273:
            raise RuntimeError(
                "LZMA param num_fast_bytes out of range: {} expected [5 - 273]!".format(num_fast_bytes))
        retval = retval + ':fb=' + str(num_fast_bytes)
    if 'num_passes' in item:
        num_passes = item['num_passes']
        if num_passes < 0 or num_passes > 1000000000:
            raise RuntimeError(
                "LZMA param num_passes out of range: {} expected [0 - 1000000000]!".format(num_passes))
        retval = retval + ':mc=' + str(num_passes)
    if 'num_lit_context_bits' in item:
        num_lit_context_bits = item['num_lit_context_bits']
        if num_lit_context_bits < 0 or num_lit_context_bits > 8:
            raise RuntimeError(
                "LZMA param num_lit_context_bits out of range: {} expected [0 - 8]!".format(num_lit_context_bits))
        retval = retval + ':lc=' + str(num_lit_context_bits)
    if 'num_lit_pos_bits' in item:
        num_lit_pos_bits = item['num_lit_pos_bits']
        if num_lit_pos_bits < 0 or num_lit_pos_bits > 4:
            raise RuntimeError(
                "LZMA param num_lit_pos_bits out of range: {} expected [0 - 4]!".format(num_lit_pos_bits))
        retval = retval + ':lp=' + str(num_lit_pos_bits)
    if 'num_pos_bits' in item:
        num_pos_bits = item['num_pos_bits']
        if num_pos_bits < 0 or num_pos_bits > 4:
            raise RuntimeError(
                "LZMA param num_pos_bits out of range: {} expected [0 - 4]!".format(num_pos_bits))
        retval = retval + ':pb=' + str(num_pos_bits)
    return retval


def build_7z_pass(type):
    pass_type = type['type']
    retval = ' -m{}={}'.format(type['id'], pass_type)

    if pass_type == 'lzma':
        retval = retval + build_7z_lzma_params(type)
    elif pass_type == 'lzma2':
        retval = retval + build_7z_lzma_params(type)
        if 'chunk_size' in type:
            retval = retval + ':c=' + type['chunk_size']
    elif pass_type == 'ppmd':
        if 'mem_usage' in type:
            retval = retval + ':mem=' + type['mem_usage']
        if 'model_order' in type:
            model_order = type['model_order']
            if model_order < 2 or model_order > 16:
                raise RuntimeError(
                    "PPMD param model_order out of range: {} expected [2 - 16]!".format(model_order))
            retval = retval + ':o=' + str(type['model_order'])
    elif pass_type == 'bcj2':
        if 'section_size' in type:
            retval = retval + ':d=' + type['section_size']
    elif pass_type == 'delta':
        if 'offset' in type:
            retval = retval + ':' + str(type['offset'])
    return retval


ANAL_TYPES = {
    'none': 0,
    'wav': 1,
    'exec': 7,
    'all': 9,
}


def build_7z_params(params):
    retval = ''
    if 'analysis' in params:
        analysis = params['analysis']
        if type(analysis) != int:
            if not analysis in ANAL_TYPES:
                raise RuntimeError(
                    "Undefined analysis type: {}!".format(analysis))
            retval = retval + ' -myx={}'.format(ANAL_TYPES[analysis])
        else:
            retval = retval + ' -myx={}'.format(analysis)
    if 'sort_by_extension' in params and params['sort_by_extension']:
        retval = retval + ' -mqs=on'
    if 'solid_mode' in params:
        solid_mode = params['solid_mode']
        use_solid = solid_mode['enabled'] if 'enabled' in solid_mode else True
        solid_command = ''
        if use_solid:
            if 'block_per_extension' in solid_mode and solid_mode['block_per_extension']:
                solid_command = solid_command + 'e'
            if 'limit_files' in solid_mode:
                solid_command = solid_command + \
                    '{}f'.format(solid_mode['limit_files'])
            if 'limit_size' in solid_mode:
                solid_command = solid_command + solid_mode['limit_size']
        else:
            solid_command = 'off'
        if solid_command:
            solid_command = ' -ms=' + solid_command
        retval = retval + solid_command
    if 'passes' in params:
        passes = params['passes']
        for i in range(len(passes)):
            cur_pass = next(
                (sub for sub in passes.values() if sub['id'] == i), None)
            retval = retval + build_7z_pass(cur_pass)
    return retval


def build_cmd_format_params(arc, job):
    arc_type = pick_dval('type', arc, job)
    if not arc_type:
        arc_type = '7z'
    if not arc_type in job:
        return ''
    if arc_type == 'zip':
        return build_zip_params(job['zip'])
    elif arc_type == '7z':
        return build_7z_params(job['7z'])
    else:
        raise RuntimeError("Undefined archive type: {}!".format(arc_type))


STRAT_TYPES = [
    'update',
    'shadow',
    'differential',
]


def build_cmd_app_idx(schema, jobs, index):
    cur_job = next((sub for sub in jobs.values() if sub['id'] == index), {})
    app_path = '"{}"'.format(schema['app_path'])
    strategy = 'u'
    archive = schema['archive']
    arc_name, patch_name = build_cmd_archive(archive, cur_job)
    strategy_type = 'update'

    if 'strategy' in schema:
        strategy_ = schema['strategy']
        if not strategy_ in STRAT_TYPES:
            print(
                "Unknown strategy: {}, using default [update].".format(strategy_))
        else:
            strategy_type = strategy_

    files = ' *' if not jobs else build_cmd_files(schema, cur_job)
    full_cmd = '{} {} "{}"{}'.format(app_path, strategy, arc_name, files)

    if 'threading' in schema:
        threading = None
        threading_ = schema['threading']
        if threading_ == 'auto':
            pass
        elif threading_ == 'none':
            threading = ' -mt=off'
        else:
            threading = ' -mt={}'.format(threading_)
        if threading:
            full_cmd = full_cmd + threading

    patch_name_retval = None

    if strategy_type == 'shadow':
        full_cmd = full_cmd + ' -uq0'
    elif strategy_type == 'differential':
        if path.exists(arc_name):
            full_cmd = full_cmd + \
                ' -u- "-up0q3r2x2y2z0w2!{}"'.format(patch_name)
            patch_name_retval = patch_name
        else:
            print('Base archive not found, switching to update mode!')

    full_cmd = full_cmd + build_cmd_pwd(archive, cur_job)
    full_cmd = full_cmd + \
        build_cmd_clevel(archive, cur_job) + \
        build_cmd_format_params(archive, cur_job)

    return (full_cmd, patch_name_retval)


def build_cmd_app(schema):
    jobs = schema['jobs'] if 'jobs' in schema else {}
    retval = []
    patches = []

    if jobs:
        includes = collect_includes(schema)
        check_inc_collisions(includes)
        schema['all_includes'] = includes
        if args.job:
            cur_job = next((sub for sub in jobs.items()
                           if sub[0] == args.job), None)
            if not cur_job:
                raise RuntimeError("Couldn't find job '{}'!".format(args.job))
            if args.verbose:
                print('Running job: ' + args.job)
            cmd, ptch = build_cmd_app_idx(schema, jobs, cur_job[1]['id'])
            retval = retval + [cmd]
            patches = patches + [ptch]
        else:
            for i in range(len(jobs)):
                cmd, ptch = build_cmd_app_idx(schema, jobs, i)
                retval = retval + [cmd]
                patches = patches + [ptch]
    else:
        cmd, ptch = build_cmd_app_idx(schema, jobs, 0)
        retval = retval + [cmd]
        patches = patches + [ptch]

    return (retval, patches)


def unit_test(filename):
    loaded_schema = yaml.safe_load_all(open(filename, 'r'))
    errors = False
    for s in loaded_schema:
        doc = load_schema(s)
        cmdline, _ = build_cmd_app(doc)
        doc_response = doc['response']
        if type(doc_response) != list:
            doc_response = [doc_response]
        error = cmdline != doc_response
        if error:
            print('Error, responses not equal for test: ' + doc['test_name'])
            difference = difflib.Differ()
            for line in difference.compare(cmdline, doc_response):
                print(line)
        errors = errors or error
    if errors:
        raise RuntimeError('Unit test failed!')
    print("Unit test completed.")


def run_stuff(filename):
    config_dir = path.dirname(path.abspath(filename))
    loaded_schema = yaml.safe_load_all(open(filename, 'r'))
    schema = load_schema(next(loaded_schema))
    if not 'scan_dir' in schema:
        schema['scan_dir'] = config_dir
    if not 'archive' in schema:
        schema['archive'] = {'out_dir': config_dir}
    elif not 'out_dir' in schema['archive']:
        schema['archive']['out_dir'] = config_dir
    cmdline, patches = build_cmd_app(schema)
    if args.verbose:
        print('Generated commands:')
        for c in cmdline:
            print(c)

    for c in range(len(cmdline)):
        cur_patch = patches[c]

        if cur_patch:
            old_patch = cur_patch + '.old'
            if path.exists(cur_patch):
                if path.exists(old_patch):
                    remove(old_patch)
                rename(cur_patch, old_patch)
        status = subprocess.run(cmdline[c], cwd=schema['scan_dir'])
        if status.returncode > 1:
            raise subprocess.CalledProcessError(status.returncode, status.args, status.stdout,
                                                status.stderr)


if args.unit_test:
    unit_test(args.config_path)
else:
    run_stuff(args.config_path)
