"""Generate exact Inno inputs from one verified immutable Windows payload."""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('session_panel_windows_build', HERE / 'build_windows.py')
BUILD = importlib.util.module_from_spec(spec)
spec.loader.exec_module(BUILD)
BOOTSTRAP_ENTRY = 'runtime/Lib/site-packages/session_panel/installation/maintenance.py'


def pascal_string(value):
    return "'" + value.replace("'", "''") + "'"


def inno_path(value):
    if any(character in value for character in '\"{}\r\n\0'):
        raise ValueError('invalid_installer_path')
    return value


def generate_includes(payload, output):
    api = BUILD.contract()
    payload, output = Path(payload), Path(output)
    if not payload.is_absolute() or not output.is_absolute() or output.exists():
        raise ValueError('new_absolute_include_output_required')
    api.no_links(payload)
    api.no_links(output)
    if output.is_relative_to(payload) or payload.is_relative_to(output):
        raise ValueError('separate_include_output_required')
    document = api.verify_payload(payload, strict_native=True)
    rows = list(document['files'])
    if BOOTSTRAP_ENTRY not in {row['path'] for row in rows}:
        raise ValueError('bootstrap_entry_missing')
    rows.append({'path': 'product.json', 'size': (payload / 'product.json').stat().st_size,
                 'sha256': api.sha256(payload / 'product.json')})
    rows.sort(key=lambda row: row['path'])
    version_id = document['version'] + '-' + document['closure'][:16]
    definitions = '\n'.join([
        '// Generated from the verified product manifest; do not edit.',
        '#define PayloadVersion "' + document['version'] + '"',
        '#define PayloadNumericVersion "' + BUILD.version_info(document['version']) + '"',
        '#define PayloadId "' + version_id + '"',
        '#define PayloadClosure "' + document['closure'] + '"',
        '',
    ])
    entries = ['; Exact immutable payload files; no stable root launcher is copied here.']
    table = ['// Expected hashes are compiled into setup and uninstall.', 'procedure LoadPayloadTable;', 'begin',
             f'  SetArrayLength(PayloadPaths, {len(rows)});', f'  SetArrayLength(PayloadHashes, {len(rows)});']
    for index, row in enumerate(rows):
        relative = inno_path(row['path'])
        path = PurePosixPath(relative)
        destination = '{app}\\versions\\' + version_id
        if str(path.parent) != '.':
            destination += '\\' + path.parent.as_posix().replace('/', '\\')
        source = inno_path(str(payload / relative))
        entries.append(f'Source: "{source}"; DestDir: "{destination}"; DestName: "{path.name}"; '
                       'Flags: ignoreversion; Check: ShouldInstallPayload')
        table.append(f'  PayloadPaths[{index}] := {pascal_string(relative.replace("/", chr(92)))};')
        table.append(f'  PayloadHashes[{index}] := {pascal_string(row["sha256"])};')
    table.extend(['end;', ''])
    texts = {'payload-defines.iss': definitions, 'payload-files.iss': '\n'.join(entries) + '\n',
             'payload-hashes.iss': '\n'.join(table)}
    output.mkdir(parents=True)
    for name, text in texts.items():
        (output / name).write_text(text, encoding='utf-8-sig')
    receipt = {'schema': 1, 'version_id': version_id, 'version': document['version'],
               'closure': document['closure'], 'files': rows,
               'includes': {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in texts}}
    (output / 'include-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = generate_includes(args.payload, args.output)
        print(json.dumps({key: receipt[key] for key in ('version_id', 'closure')}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as error:
        print('installer_include_failed:' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
