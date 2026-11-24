#!/usr/bin/env python3
"""Проверка сборки каждого коммита диапазона.

Инструмент для контроля реструктуризации истории (squash/rebase): подтверждает,
что каждый промежуточный коммит диапазона собирается и проходит тесты, а не
только его вершина. Полный слепок проекта на каждом коммите собирается в
отдельном git worktree, поэтому основное рабочее дерево и его каталог build/
не затрагиваются.

Прерывается на первом упавшем коммите и оставляет его worktree на диске
для диагностики (путь выводится в отчёте).

Примеры (запуск из корня проверяемого репозитория):
    python3 /path/to/verify_commit_range/verify_commit_range.py main..HEAD \
        --preset host_unit_tests
    python3 /path/to/verify_commit_range/verify_commit_range.py main..HEAD \
        --preset host_unit_tests --preset firmware_release --with-tests
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Сколько последних строк лога показывать в отчёте об упавшей команде.
LOG_TAIL_LINES = 15


class PresetError(Exception):
    """Сборка пресета завершилась с ошибкой (в поле preset — имя пресета)."""

    def __init__(self, preset: str, log_file: Path):
        super().__init__(preset)
        self.preset = preset
        self.log_file = log_file


def run_logged(cmd: list[str], cwd: Path, log_file: Path) -> None:
    """Выполняет команду с выводом в консоль и полным логом в файл."""
    print(f"  {' '.join(cmd)}", flush=True)
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(cmd)}\n")
        log.flush()
        subprocess.run(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True)


def check_prerequisites() -> None:
    """Проверяет наличие обязательных инструментов."""
    for tool in ["git", "cmake", "ninja"]:
        if shutil.which(tool) is None:
            raise RuntimeError(f"Не найден обязательный инструмент: {tool}")


def commit_list(git_range: str) -> list[str]:
    """Возвращает список коммитов диапазона в порядке применения (от base к tip)."""
    result = subprocess.run(
        ["git", "rev-list", "--reverse", git_range],
        check=True,
        capture_output=True,
        text=True,
    )
    commits = result.stdout.split()
    if not commits:
        raise RuntimeError(f"Диапазон '{git_range}' не содержит коммитов")
    return commits


def commit_subject(sha: str) -> str:
    """Возвращает заголовок сообщения коммита."""
    result = subprocess.run(
        ["git", "show", "-s", "--format=%s", sha],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def add_worktree(sha: str, parent_dir: Path) -> Path:
    """Создаёт detached worktree на коммите и возвращает путь к нему."""
    worktree = Path(tempfile.mkdtemp(prefix="commit_", dir=parent_dir))
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), sha],
        check=True,
        capture_output=True,
        text=True,
    )
    return worktree


def remove_worktree(worktree: Path) -> bool:
    """Удаляет worktree; на Windows каталог может быть временно занят —
    даём несколько попыток (аналогично build.py). Возвращает успех удаления."""
    for attempt in range(3):
        result = subprocess.run(["git", "worktree", "remove", "--force", str(worktree)])
        if result.returncode == 0:
            return True
        if attempt < 2:
            import time

            time.sleep(2)
    return False


def report_failure(sha: str, error: PresetError, worktree: Path | None) -> None:
    """Выводит диагностику упавшего коммита."""
    print(
        f"\nКоммит {sha[:9]} НЕ прошёл проверку (пресет '{error.preset}').",
        file=sys.stderr,
    )
    print(f"Полный лог: {error.log_file}\nПоследние строки:", file=sys.stderr)
    tail_lines = error.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(tail_lines[-LOG_TAIL_LINES:]), file=sys.stderr)
    if worktree is not None:
        print(
            f"\nWorktree сохранён для диагностики: {worktree}\n"
            f"Удаление после разбора: git worktree remove --force {worktree}",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверка сборки и тестов каждого коммита диапазона "
        "(git-диапазон вида base..tip)"
    )
    parser.add_argument(
        "range",
        help="Диапазон коммитов, например develop..HEAD",
    )
    parser.add_argument(
        "--preset",
        action="append",
        required=True,
        help="Собрать указанный пресет (обязательный аргумент, "
        "можно указать несколько раз)",
    )
    parser.add_argument(
        "--with-tests",
        action="store_true",
        help="Прогонять ctest после сборки всех указанных пресетов",
    )
    args = parser.parse_args()

    presets = args.preset

    try:
        check_prerequisites()
        commits = commit_list(args.range)
    except RuntimeError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1

    print(f"Проверка {len(commits)} коммитов, пресеты: {', '.join(presets)}")

    for index, sha in enumerate(commits, start=1):
        staging = Path(tempfile.mkdtemp(prefix="verify_commit_range_"))
        worktree: Path | None = None
        try:
            print(f"\n=== {index}/{len(commits)} ===", flush=True)
            print(f"[{sha[:9]}] {commit_subject(sha)}", flush=True)
            worktree = add_worktree(sha, staging)
            logs_dir = worktree / "_verify_logs"
            logs_dir.mkdir()

            for preset in presets:
                log_file = logs_dir / f"{preset}.log"
                try:
                    run_logged(["cmake", "--preset", preset], worktree, log_file)
                    run_logged(["cmake", "--build", f"build/{preset}"], worktree, log_file)
                    if args.with_tests:
                        run_logged(
                            [
                                "ctest",
                                "--test-dir",
                                f"build/{preset}",
                                "--schedule-random",
                                "--output-on-failure",
                            ],
                            worktree,
                            log_file,
                        )
                except subprocess.CalledProcessError:
                    raise PresetError(preset, log_file)

            if not remove_worktree(worktree):
                print(
                    f"Предупреждение: не удалось удалить worktree {worktree}",
                    file=sys.stderr,
                )
            shutil.rmtree(staging, ignore_errors=True)
        except PresetError as e:
            report_failure(sha, e, worktree)
            return 1
        except subprocess.CalledProcessError as e:
            print(f"Ошибка git: {e}", file=sys.stderr)
            return 1

    print(f"\nВсе {len(commits)} коммитов прошли проверку.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
