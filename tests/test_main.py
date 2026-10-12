from __future__ import annotations

import os
import runpy
import stat
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import main as mergepy

MAIN_SCRIPT = Path(mergepy.__file__)


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    directory = tmp_path / 'project'
    directory.mkdir()
    return directory


@pytest.fixture
def reader() -> MagicMock:
    mock = MagicMock(spec=mergepy.SourceReader)
    mock.read.side_effect = lambda path: f'# {path.name}'
    return mock


@pytest.fixture
def combiner(reader: MagicMock) -> mergepy.PythonFileCombiner:
    return mergepy.PythonFileCombiner(reader)


def write_source(directory: Path, name: str, data: bytes = b'pass\n') -> Path:
    path = directory / name
    path.write_bytes(data)
    return path


def run_concurrent_combines(source_dir: Path, output_path: Path) -> tuple[list[str], list[BaseException]]:
    """2つの結合処理を、両方が一時ファイルを開いた状態で重ねて実行する。"""
    tags = ('first', 'second')
    # リーダーは一時ファイルを開いた後に呼ばれるため、ここで待ち合わせると書き込みが必ず重なる
    barrier = threading.Barrier(len(tags), timeout=10)
    errors: list[BaseException] = []

    def combine(tag: str) -> None:
        def read(path: Path) -> str:
            barrier.wait()
            return f'# {tag}\n' * 2000

        mock = MagicMock(spec=mergepy.SourceReader)
        mock.read.side_effect = read
        try:
            mergepy.PythonFileCombiner(mock).combine(source_dir, output_path)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=combine, args=(tag,)) for tag in tags]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    expected = [mergepy.PythonFileCombiner.format_block('a.py', f'# {tag}\n' * 2000) for tag in tags]
    return expected, errors


def failing_reader(failing_name: str) -> MagicMock:
    def read(path: Path) -> str:
        if path.name == failing_name:
            raise mergepy.SourceDecodeError(path, 'invalid start byte')
        return f'# {path.name}'

    mock = MagicMock(spec=mergepy.SourceReader)
    mock.read.side_effect = read
    return mock


# SourceReader.read


def test正常系_readメソッドがUTF8の日本語を読み込むこと(source_dir: Path):
    path = write_source(source_dir, 'a.py', 'print("こんにちは")\n'.encode())

    assert mergepy.SourceReader().read(path) == 'print("こんにちは")\n'


def test正常系_readメソッドがBOMを本文から取り除くこと(source_dir: Path):
    path = write_source(source_dir, 'a.py', b'\xef\xbb\xbfprint(1)\n')

    assert mergepy.SourceReader().read(path) == 'print(1)\n'


def test正常系_readメソッドがエンコーディング宣言に従って読み込むこと(source_dir: Path):
    path = write_source(source_dir, 'a.py', '# -*- coding: cp932 -*-\nx = "あ"\n'.encode('cp932'))

    assert mergepy.SourceReader().read(path) == '# -*- coding: cp932 -*-\nx = "あ"\n'


def test正常系_readメソッドがCRLFをLFとして読み込むこと(source_dir: Path):
    path = write_source(source_dir, 'a.py', b'a = 1\r\nb = 2\r\n')

    assert mergepy.SourceReader().read(path) == 'a = 1\nb = 2\n'


def test正常系_readメソッドが宣言のないファイルをフォールバック文字コードで読み込むこと(source_dir: Path):
    path = write_source(source_dir, 'a.py', 'x = "あ"\n'.encode('cp932'))

    assert mergepy.SourceReader(fallback_encoding='cp932').read(path) == 'x = "あ"\n'


def test正常系_readメソッドがフォールバック指定時もUTF8を優先して読み込むこと(source_dir: Path):
    path = write_source(source_dir, 'a.py', 'x = "あ"\n'.encode())

    assert mergepy.SourceReader(fallback_encoding='cp932').read(path) == 'x = "あ"\n'


def test異常系_readメソッドがUTF8でないファイルでSourceDecodeErrorを送出すること(source_dir: Path):
    path = write_source(source_dir, 'a.py', 'x = "あ"\n'.encode('cp932'))

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.SourceReader().read(path)


def test異常系_readメソッドが3行目以降の不正なバイトでSourceDecodeErrorを送出すること(source_dir: Path):
    path = write_source(source_dir, 'a.py', b'a = 1\nb = 2\n' + 'x = "あ"\n'.encode('cp932'))

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.SourceReader().read(path)


def test異常系_readメソッドが読めないファイルの名前を含むSourceDecodeErrorを送出すること(source_dir: Path):
    path = write_source(source_dir, 'broken.py', 'x = "あ"\n'.encode('cp932'))

    with pytest.raises(mergepy.SourceDecodeError, match=r'broken\.py'):
        mergepy.SourceReader().read(path)


def test異常系_readメソッドが未知のエンコーディング宣言でSourceDecodeErrorを送出すること(source_dir: Path):
    path = write_source(source_dir, 'a.py', b'# -*- coding: no-such-codec -*-\n')

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.SourceReader().read(path)


def test異常系_readメソッドがフォールバックでも読めないファイルでSourceDecodeErrorを送出すること(source_dir: Path):
    path = write_source(source_dir, 'a.py', 'x = "あ"\n'.encode('cp932'))

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.SourceReader(fallback_encoding='ascii').read(path)


def test異常系_readメソッドが未知のフォールバック文字コードでSourceDecodeErrorを送出すること(source_dir: Path):
    path = write_source(source_dir, 'a.py', 'x = "あ"\n'.encode('cp932'))

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.SourceReader(fallback_encoding='no-such-codec').read(path)


def test異常系_readメソッドが存在しないファイルでFileNotFoundErrorを送出すること(source_dir: Path):
    with pytest.raises(FileNotFoundError):
        mergepy.SourceReader().read(source_dir / 'missing.py')


# PythonFileCombiner.find_sources


def test正常系_find_sourcesメソッドが作成順に関係なくファイル名順で返すこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    for name in ('c.py', 'a.py', 'B.py'):
        write_source(source_dir, name)

    assert [path.name for path in combiner.find_sources(source_dir)] == ['a.py', 'B.py', 'c.py']


def test正常系_find_sourcesメソッドがサブディレクトリ内のファイルを含めないこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    (source_dir / 'sub').mkdir()
    write_source(source_dir / 'sub', 'nested.py')

    assert [path.name for path in combiner.find_sources(source_dir)] == ['a.py']


def test正常系_find_sourcesメソッドがpyで終わる名前のディレクトリを除外すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    (source_dir / 'package.py').mkdir()

    assert [path.name for path in combiner.find_sources(source_dir)] == ['a.py']


def test正常系_find_sourcesメソッドがpy以外の拡張子を除外すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    for name in ('a.py', 'b.pyw', 'c.txt', 'd.py.bak'):
        write_source(source_dir, name)

    assert [path.name for path in combiner.find_sources(source_dir)] == ['a.py']


def test正常系_find_sourcesメソッドが大文字の拡張子を従来どおり除外すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    write_source(source_dir, 'UPPER.PY')

    assert [path.name for path in combiner.find_sources(source_dir)] == ['a.py']


def test正常系_find_sourcesメソッドがexcludeに指定したファイルを除外すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    excluded = write_source(source_dir, 'combined.py')

    assert [path.name for path in combiner.find_sources(source_dir, exclude=excluded)] == ['a.py']


def test正常系_find_sourcesメソッドがpyファイルのないディレクトリで空リストを返すこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'memo.txt')

    assert combiner.find_sources(source_dir) == []


def test異常系_find_sourcesメソッドが存在しないディレクトリでFileNotFoundErrorを送出すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    with pytest.raises(FileNotFoundError):
        combiner.find_sources(source_dir / 'missing')


# PythonFileCombiner.format_block


def test正常系_format_blockメソッドが従来の区切り書式で整形すること():
    assert mergepy.PythonFileCombiner.format_block('a.py', 'print(1)\n') == '```a.py\nprint(1)\n\n```\n'


def test正常系_format_blockメソッドが空の内容を従来の区切り書式で整形すること():
    assert mergepy.PythonFileCombiner.format_block('a.py', '') == '```a.py\n\n```\n'


def test正常系_format_blockメソッドがバッククォート3個を含む内容を4個の区切りで囲むこと():
    assert mergepy.PythonFileCombiner.format_block('a.py', 'x = "```"') == '````a.py\nx = "```"\n````\n'


def test正常系_format_blockメソッドが最長のバッククォート列より1個長い区切りで囲むこと():
    assert mergepy.PythonFileCombiner.format_block('a.py', '``` `````').startswith('``````a.py\n')


def test正常系_format_blockメソッドが2個以下のバッククォートでは従来の区切りを使うこと():
    assert mergepy.PythonFileCombiner.format_block('a.py', 'x = "``"') == '```a.py\nx = "``"\n```\n'


# PythonFileCombiner.combine


def test正常系_combineメソッドが各ファイルをファイル名順に結合して書き出すこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'b.py')
    write_source(source_dir, 'a.py')
    output_path = source_dir / 'out.txt'

    combiner.combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == '```a.py\n# a.py\n```\n```b.py\n# b.py\n```\n'


def test正常系_combineメソッドが結合したファイルの一覧を返すこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'b.py')
    write_source(source_dir, 'a.py')

    sources = combiner.combine(source_dir, source_dir / 'out.txt')

    assert sources == [source_dir / 'a.py', source_dir / 'b.py']


def test正常系_combineメソッドがリーダーへ各ファイルを1回ずつ渡すこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner, reader: MagicMock
):
    write_source(source_dir, 'b.py')
    write_source(source_dir, 'a.py')

    combiner.combine(source_dir, source_dir / 'out.txt')

    assert [call.args[0].name for call in reader.read.call_args_list] == ['a.py', 'b.py']


def test正常系_combineメソッドがOSの改行コードで書き出すこと(source_dir: Path, combiner: mergepy.PythonFileCombiner):
    write_source(source_dir, 'a.py')
    output_path = source_dir / 'out.txt'

    combiner.combine(source_dir, output_path)

    assert output_path.read_bytes() == '```a.py\n# a.py\n```\n'.replace('\n', os.linesep).encode()


def test正常系_combineメソッドがBOMなしのUTF8で書き出すこと(source_dir: Path):
    write_source(source_dir, 'a.py', 'x = "あ"'.encode())
    output_path = source_dir / 'out.txt'

    mergepy.PythonFileCombiner().combine(source_dir, output_path)

    assert output_path.read_bytes() == '```a.py\nx = "あ"\n```\n'.replace('\n', os.linesep).encode('utf-8')


def test正常系_combineメソッドが既存の出力ファイルを置き換えること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    output_path = source_dir / 'out.txt'
    output_path.write_text('OLD', encoding='utf-8')

    combiner.combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == '```a.py\n# a.py\n```\n'


def test正常系_combineメソッドが一時ファイルを残さないこと(source_dir: Path, combiner: mergepy.PythonFileCombiner):
    write_source(source_dir, 'a.py')

    combiner.combine(source_dir, source_dir / 'out.txt')

    assert sorted(path.name for path in source_dir.iterdir()) == ['a.py', 'out.txt']


def test正常系_combineメソッドが出力ファイル自身を結合対象に含めないこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    output_path = write_source(source_dir, 'combined.py', b'OLD')

    sources = combiner.combine(source_dir, output_path)

    assert sources == [source_dir / 'a.py']


def test正常系_combineメソッドがリーダー未指定でも実ファイルを結合すること(source_dir: Path):
    write_source(source_dir, 'a.py', b'print(1)\n')
    output_path = source_dir / 'out.txt'

    mergepy.PythonFileCombiner().combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == '```a.py\nprint(1)\n\n```\n'


def test異常系_combineメソッドがpyファイルのないディレクトリでNoSourceFilesErrorを送出すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    with pytest.raises(mergepy.NoSourceFilesError):
        combiner.combine(source_dir, source_dir / 'out.txt')


def test異常系_combineメソッドがNoSourceFilesError送出時に既存の出力を保持すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    output_path = source_dir / 'out.txt'
    output_path.write_text('OLD', encoding='utf-8')

    with pytest.raises(mergepy.NoSourceFilesError):
        combiner.combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == 'OLD'


def test異常系_combineメソッドがリーダーのSourceDecodeErrorをそのまま送出すること(source_dir: Path):
    write_source(source_dir, 'a.py')
    write_source(source_dir, 'b.py')

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.PythonFileCombiner(failing_reader('b.py')).combine(source_dir, source_dir / 'out.txt')


def test異常系_combineメソッドがSourceDecodeError送出時に既存の出力を保持すること(source_dir: Path):
    write_source(source_dir, 'a.py')
    write_source(source_dir, 'b.py')
    output_path = source_dir / 'out.txt'
    output_path.write_text('OLD', encoding='utf-8')

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.PythonFileCombiner(failing_reader('b.py')).combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == 'OLD'


def test異常系_combineメソッドがSourceDecodeError送出時に一時ファイルを残さないこと(source_dir: Path):
    write_source(source_dir, 'a.py')
    write_source(source_dir, 'b.py')

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.PythonFileCombiner(failing_reader('b.py')).combine(source_dir, source_dir / 'out.txt')

    assert sorted(path.name for path in source_dir.iterdir()) == ['a.py', 'b.py']


def test異常系_combineメソッドが置き換え失敗時にPermissionErrorを送出すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')

    with patch.object(mergepy.os, 'replace', side_effect=PermissionError('locked')):
        with pytest.raises(PermissionError):
            combiner.combine(source_dir, source_dir / 'out.txt')


def test異常系_combineメソッドがPermissionError送出時に一時ファイルを残さないこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')

    with patch.object(mergepy.os, 'replace', side_effect=PermissionError('locked')):
        with pytest.raises(PermissionError):
            combiner.combine(source_dir, source_dir / 'out.txt')

    assert sorted(path.name for path in source_dir.iterdir()) == ['a.py']


def test異常系_combineメソッドがPermissionError送出時に既存の出力を保持すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    output_path = source_dir / 'out.txt'
    output_path.write_text('OLD', encoding='utf-8')

    with patch.object(mergepy.os, 'replace', side_effect=PermissionError('locked')):
        with pytest.raises(PermissionError):
            combiner.combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == 'OLD'


def test正常系_combineメソッドが出力先と同じディレクトリに一時ファイルを作ること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner, tmp_path: Path
):
    write_source(source_dir, 'a.py')
    output_dir = tmp_path / 'output'
    output_dir.mkdir()

    with patch.object(mergepy.tempfile, 'mkstemp', wraps=mergepy.tempfile.mkstemp) as mkstemp:
        combiner.combine(source_dir, output_dir / 'out.txt')

    assert mkstemp.call_args.kwargs['dir'] == output_dir


def test正常系_combineメソッドが出力ファイル名を含まない固定の接頭辞で一時ファイルを作ること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')

    with patch.object(mergepy.tempfile, 'mkstemp', wraps=mergepy.tempfile.mkstemp) as mkstemp:
        combiner.combine(source_dir, source_dir / 'out.txt')

    assert mkstemp.call_args.kwargs['prefix'] == '.mergepy-'


@pytest.mark.skipif(os.name == 'nt', reason='Windowsではパス全体の長さの上限に先に達しうる')
def test正常系_combineメソッドが上限に近い長さの出力ファイル名でも結合結果を書き出すこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    output_path = source_dir / ('a' * 240 + '.txt')

    combiner.combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == '```a.py\n# a.py\n```\n'


def test正常系_combineメソッドが出力先と同名のtmpファイルを書き換えないこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    existing = source_dir / 'out.txt.tmp'
    existing.write_text('KEEP', encoding='utf-8')

    combiner.combine(source_dir, source_dir / 'out.txt')

    assert existing.read_text(encoding='utf-8') == 'KEEP'


def test正常系_combineメソッドが出力先と同名のtmpファイルがあっても結合結果を書き出すこと(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    (source_dir / 'out.txt.tmp').write_text('KEEP', encoding='utf-8')
    output_path = source_dir / 'out.txt'

    combiner.combine(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') == '```a.py\n# a.py\n```\n'


def test正常系_combineメソッドが同時実行時にどちらか一方の完全な結果を書き出すこと(source_dir: Path):
    write_source(source_dir, 'a.py')
    output_path = source_dir / 'out.txt'

    expected, _ = run_concurrent_combines(source_dir, output_path)

    assert output_path.read_text(encoding='utf-8') in expected


def test正常系_combineメソッドが同時実行時に一時ファイルを残さないこと(source_dir: Path):
    write_source(source_dir, 'a.py')

    run_concurrent_combines(source_dir, source_dir / 'out.txt')

    assert sorted(path.name for path in source_dir.iterdir()) == ['a.py', 'out.txt']


@pytest.mark.skipif(os.name == 'nt', reason='Windowsでは開いている出力先への同時置き換えが共有違反になりうる')
def test正常系_combineメソッドが同時実行時にどちらの処理も失敗しないこと(source_dir: Path):
    write_source(source_dir, 'a.py')

    _, errors = run_concurrent_combines(source_dir, source_dir / 'out.txt')

    assert errors == []


@pytest.mark.skipif(os.name != 'posix', reason='ファイル権限はPOSIXだけで調整する')
def test正常系_combineメソッドが新規の出力を所有者のみの権限で作成すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')
    output_path = source_dir / 'out.txt'

    combiner.combine(source_dir, output_path)

    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600


def test正常系_combineメソッドがプロセスのumaskを変更しないこと(source_dir: Path, combiner: mergepy.PythonFileCombiner):
    write_source(source_dir, 'a.py')

    with patch.object(mergepy.os, 'umask') as umask:
        combiner.combine(source_dir, source_dir / 'out.txt')

    umask.assert_not_called()


@pytest.mark.skipif(os.name != 'posix', reason='ファイル権限はPOSIXだけで調整する')
def test正常系_combineメソッドが既存の出力の権限を引き継ぐこと(source_dir: Path, combiner: mergepy.PythonFileCombiner):
    write_source(source_dir, 'a.py')
    output_path = source_dir / 'out.txt'
    output_path.write_text('OLD', encoding='utf-8')
    output_path.chmod(0o640)

    combiner.combine(source_dir, output_path)

    assert stat.S_IMODE(output_path.stat().st_mode) == 0o640


def test異常系_combineメソッドがSourceDecodeError送出時に出力先と同名のtmpファイルを削除しないこと(source_dir: Path):
    write_source(source_dir, 'a.py')
    existing = source_dir / 'out.txt.tmp'
    existing.write_text('KEEP', encoding='utf-8')

    with pytest.raises(mergepy.SourceDecodeError):
        mergepy.PythonFileCombiner(failing_reader('a.py')).combine(source_dir, source_dir / 'out.txt')

    assert existing.read_text(encoding='utf-8') == 'KEEP'


def test異常系_combineメソッドが出力先のディレクトリがないときFileNotFoundErrorを送出すること(
    source_dir: Path, combiner: mergepy.PythonFileCombiner
):
    write_source(source_dir, 'a.py')

    with pytest.raises(FileNotFoundError):
        combiner.combine(source_dir, source_dir / 'missing' / 'out.txt')


# create_combined_python_file


def test正常系_create_combined_python_fileメソッドがカレントディレクトリのファイルを結合すること(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    write_source(source_dir, 'a.py', b'print(1)\n')
    monkeypatch.chdir(source_dir)

    mergepy.create_combined_python_file('out.txt')

    assert (source_dir / 'out.txt').read_text(encoding='utf-8') == '```a.py\nprint(1)\n\n```\n'


def test正常系_create_combined_python_fileメソッドが渡された出力ファイル名を返すこと(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    write_source(source_dir, 'a.py')
    monkeypatch.chdir(source_dir)

    assert mergepy.create_combined_python_file('out.txt') == 'out.txt'


def test正常系_create_combined_python_fileメソッドがカレントディレクトリと出力先を結合処理へ渡すこと():
    with patch.object(mergepy.PythonFileCombiner, 'combine') as combine:
        mergepy.create_combined_python_file('out.txt')

    combine.assert_called_once_with(Path('.'), Path('out.txt'))


def test異常系_create_combined_python_fileメソッドがpyファイルのないときNoSourceFilesErrorを送出すること(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(source_dir)

    with pytest.raises(mergepy.NoSourceFilesError):
        mergepy.create_combined_python_file('out.txt')


# build_parser


def test正常系_build_parserメソッドが引数なしでカレントディレクトリを対象にすること():
    assert mergepy.build_parser().parse_args([]).directory == '.'


def test正常系_build_parserメソッドが引数なしで出力先を未指定にすること():
    assert mergepy.build_parser().parse_args([]).output is None


def test正常系_build_parserメソッドが引数なしでフォールバック文字コードを未指定にすること():
    assert mergepy.build_parser().parse_args([]).fallback_encoding is None


def test正常系_build_parserメソッドが対象ディレクトリを受け取ること():
    assert mergepy.build_parser().parse_args(['src']).directory == 'src'


def test正常系_build_parserメソッドが出力先オプションを受け取ること():
    assert mergepy.build_parser().parse_args(['-o', 'out.txt']).output == 'out.txt'


def test正常系_build_parserメソッドがフォールバック文字コードを受け取ること():
    assert mergepy.build_parser().parse_args(['--fallback-encoding', 'cp932']).fallback_encoding == 'cp932'


def test異常系_build_parserメソッドが未知のオプションでSystemExitを送出すること():
    with pytest.raises(SystemExit):
        mergepy.build_parser().parse_args(['--unknown'])


# main


def test正常系_mainメソッドが引数なしでカレントディレクトリに既定の名前で書き出すこと(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    write_source(source_dir, 'a.py', b'print(1)\n')
    monkeypatch.chdir(source_dir)

    mergepy.main([])

    assert (source_dir / 'Pythonファイル内容全文.txt').read_text(encoding='utf-8') == '```a.py\nprint(1)\n\n```\n'


def test正常系_mainメソッドが成功時に0を返すこと(source_dir: Path):
    write_source(source_dir, 'a.py')

    assert mergepy.main([str(source_dir)]) == 0


def test正常系_mainメソッドがまとめたファイル数を標準出力に表示すること(
    source_dir: Path, capsys: pytest.CaptureFixture[str]
):
    write_source(source_dir, 'a.py')
    write_source(source_dir, 'b.py')

    mergepy.main([str(source_dir)])

    assert '2 個の .py ファイル' in capsys.readouterr().out


def test正常系_mainメソッドが指定ディレクトリの中に既定の名前で書き出すこと(source_dir: Path):
    write_source(source_dir, 'a.py')

    mergepy.main([str(source_dir)])

    assert (source_dir / mergepy.DEFAULT_OUTPUT_FILENAME).is_file()


def test正常系_mainメソッドが出力先オプションの場所へ書き出すこと(source_dir: Path, tmp_path: Path):
    write_source(source_dir, 'a.py')
    output_path = tmp_path / 'custom.txt'

    mergepy.main([str(source_dir), '-o', str(output_path)])

    assert output_path.is_file()


def test正常系_mainメソッドが出力先オプション指定時に既定の出力を作らないこと(source_dir: Path, tmp_path: Path):
    write_source(source_dir, 'a.py')

    mergepy.main([str(source_dir), '-o', str(tmp_path / 'custom.txt')])

    assert not (source_dir / mergepy.DEFAULT_OUTPUT_FILENAME).exists()


def test正常系_mainメソッドがフォールバック文字コードでファイルを結合すること(source_dir: Path):
    write_source(source_dir, 'a.py', 'x = "あ"'.encode('cp932'))

    mergepy.main([str(source_dir), '--fallback-encoding', 'cp932'])

    assert (source_dir / mergepy.DEFAULT_OUTPUT_FILENAME).read_text(encoding='utf-8') == '```a.py\nx = "あ"\n```\n'


def test正常系_mainメソッドが引数未指定時にコマンドライン引数を使うこと(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    write_source(source_dir, 'a.py')
    monkeypatch.setattr(mergepy.sys, 'argv', ['main.py', str(source_dir)])

    mergepy.main()

    assert (source_dir / mergepy.DEFAULT_OUTPUT_FILENAME).is_file()


def test異常系_mainメソッドが読めないファイルがあるとき1を返すこと(source_dir: Path):
    write_source(source_dir, 'a.py', 'x = "あ"'.encode('cp932'))

    assert mergepy.main([str(source_dir)]) == 1


def test異常系_mainメソッドが読めないファイルの名前を標準エラーに表示すること(
    source_dir: Path, capsys: pytest.CaptureFixture[str]
):
    write_source(source_dir, 'broken.py', 'x = "あ"'.encode('cp932'))

    mergepy.main([str(source_dir)])

    assert 'broken.py' in capsys.readouterr().err


def test異常系_mainメソッドが読めないファイルがあるとき出力を作らないこと(source_dir: Path):
    write_source(source_dir, 'a.py', 'x = "あ"'.encode('cp932'))

    mergepy.main([str(source_dir)])

    assert sorted(path.name for path in source_dir.iterdir()) == ['a.py']


def test異常系_mainメソッドがpyファイルのないディレクトリで1を返すこと(source_dir: Path):
    assert mergepy.main([str(source_dir)]) == 1


def test異常系_mainメソッドが存在しないディレクトリで1を返すこと(source_dir: Path):
    assert mergepy.main([str(source_dir / 'missing')]) == 1


def test異常系_mainメソッドが書き込み失敗時に1を返すこと(source_dir: Path):
    write_source(source_dir, 'a.py')

    with patch.object(mergepy.os, 'replace', side_effect=PermissionError('locked')):
        assert mergepy.main([str(source_dir)]) == 1


# スクリプトとしての読み込み・実行


def test正常系_mainメソッドがimportだけでは実行されずファイルを書き出さないこと(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    write_source(source_dir, 'a.py')
    monkeypatch.chdir(source_dir)

    runpy.run_path(str(MAIN_SCRIPT), run_name='imported')

    assert sorted(path.name for path in source_dir.iterdir()) == ['a.py']


def test正常系_mainメソッドがスクリプト実行時に終了コード0で終了すること(
    source_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    write_source(source_dir, 'a.py')
    monkeypatch.setattr('sys.argv', ['main.py', str(source_dir)])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(MAIN_SCRIPT), run_name='__main__')

    assert exit_info.value.code == 0
