from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
import tokenize
from collections.abc import Sequence
from pathlib import Path

DEFAULT_OUTPUT_FILENAME = 'Pythonファイル内容全文.txt'
SOURCE_SUFFIX = '.py'
MIN_FENCE_LENGTH = 3
TEMPORARY_PREFIX = '.mergepy-'
TEMPORARY_SUFFIX = '.tmp'


class MergePyError(Exception):
    """トレースバックなしで利用者へ表示するエラー。"""


class SourceDecodeError(MergePyError):
    def __init__(self, path: Path, reason: str):
        super().__init__(f'{path.name} を読み込めません: {reason}')
        self.path = path


class NoSourceFilesError(MergePyError):
    def __init__(self, directory: Path):
        super().__init__(f'{directory} に {SOURCE_SUFFIX} ファイルがありません')
        self.directory = directory


class SourceReader:
    """Pythonソースファイルを、そのファイル自身が宣言する文字コードで読み込む。"""

    def __init__(self, fallback_encoding: str | None = None):
        self.fallback_encoding = fallback_encoding

    def read(self, path: Path) -> str:
        # BOMとPEP 263の宣言で文字コードを決め、どちらもなければUTF-8とする
        try:
            with path.open('rb') as f:
                encoding, _ = tokenize.detect_encoding(f.readline)
            return path.read_text(encoding=encoding)
        except (SyntaxError, UnicodeDecodeError) as error:
            if self.fallback_encoding is None:
                raise SourceDecodeError(path, str(error)) from error

        try:
            return path.read_text(encoding=self.fallback_encoding)
        except (LookupError, UnicodeDecodeError) as error:
            raise SourceDecodeError(path, str(error)) from error


class PythonFileCombiner:
    """ディレクトリ直下の .py ファイルを1つのテキストファイルにまとめる。"""

    def __init__(self, reader: SourceReader | None = None):
        self.reader = reader or SourceReader()

    def find_sources(self, directory: Path, exclude: Path | None = None) -> list[Path]:
        excluded = exclude.resolve() if exclude is not None else None
        sources = [
            path
            for path in directory.iterdir()
            if path.name.endswith(SOURCE_SUFFIX) and path.is_file() and path.resolve() != excluded
        ]
        # 列挙順はファイルシステムによって異なるため、名前順に固定する
        return sorted(sources, key=lambda path: (path.name.casefold(), path.name))

    @staticmethod
    def format_block(name: str, content: str) -> str:
        # 内容の途中で区切りが閉じないよう、最長のバッククォート列より長い区切りを使う
        longest_run = max((len(run) for run in re.findall(r'`+', content)), default=0)
        fence = '`' * max(MIN_FENCE_LENGTH, longest_run + 1)
        return f'{fence}{name}\n{content}\n{fence}\n'

    @staticmethod
    def _apply_output_mode(fd: int, output_path: Path) -> None:
        # 既存の出力があればその権限を引き継ぐ。新規の出力は mkstemp が作る所有者のみの権限のままにする
        if os.name != 'posix':
            return
        try:
            mode = stat.S_IMODE(output_path.stat().st_mode)
        except FileNotFoundError:
            return
        os.fchmod(fd, mode)

    def combine(self, directory: Path, output_path: Path) -> list[Path]:
        sources = self.find_sources(directory, exclude=output_path)
        if not sources:
            raise NoSourceFilesError(directory)

        # 失敗しても前回の出力が残るよう、一時ファイルへ書いてから置き換える。
        # 一時ファイルは実行ごとに一意な名前で排他的に作成し、既存ファイルや並行実行と衝突させない。
        # 出力ファイル名が長くても名前の長さの上限を超えないよう、固定の短い接頭辞を使う
        fd, temporary_name = tempfile.mkstemp(dir=output_path.parent, prefix=TEMPORARY_PREFIX, suffix=TEMPORARY_SUFFIX)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                self._apply_output_mode(f.fileno(), output_path)
                for source in sources:
                    f.write(self.format_block(source.name, self.reader.read(source)))
            os.replace(temporary_path, output_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        return sources


def create_combined_python_file(output_filename: str) -> str:
    """旧バージョン互換の関数。カレントディレクトリ直下の .py ファイルをまとめ、渡された出力ファイル名を返す。"""
    PythonFileCombiner().combine(Path('.'), Path(output_filename))
    return output_filename


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='ディレクトリ直下の .py ファイルを1つのテキストファイルにまとめます。')
    parser.add_argument(
        'directory',
        nargs='?',
        default='.',
        help='対象ディレクトリ (既定: カレントディレクトリ)',
    )
    parser.add_argument(
        '-o',
        '--output',
        help=f'出力ファイル (既定: 対象ディレクトリの {DEFAULT_OUTPUT_FILENAME})',
    )
    parser.add_argument(
        '--fallback-encoding',
        help='UTF-8 でもエンコーディング宣言でも読めないファイルに使う文字コード (例: cp932)',
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # コンソールで表示できないファイル名のせいで、完了した処理を異常終了させない
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='backslashreplace')

    args = build_parser().parse_args(argv)
    directory = Path(args.directory)
    output_path = Path(args.output) if args.output else directory / DEFAULT_OUTPUT_FILENAME
    combiner = PythonFileCombiner(SourceReader(args.fallback_encoding))

    try:
        sources = combiner.combine(directory, output_path)
    except (MergePyError, OSError) as error:
        print(f'エラー: {error}', file=sys.stderr)
        return 1

    print(f'{len(sources)} 個の {SOURCE_SUFFIX} ファイルを {output_path} にまとめました')
    return 0


if __name__ == '__main__':
    sys.exit(main())
