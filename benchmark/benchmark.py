#!/usr/bin/env python

import datetime
import json
import os
import random
import re
import shutil
import subprocess
import time
from collections import defaultdict
from json.decoder import JSONDecodeError
from pathlib import Path

import git
import lox
import typer
from rich.console import Console

from aider import models
from aider.coders import Coder
from aider.dump import dump  # noqa: F401
from aider.io import InputOutput

BENCHMARK_DNAME = Path("tmp.benchmark/.")
assert BENCHMARK_DNAME.exists() and BENCHMARK_DNAME.is_dir()

ORIGINAL_DNAME = BENCHMARK_DNAME / "practice/."
assert ORIGINAL_DNAME.exists() and ORIGINAL_DNAME.is_dir()

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


@app.command()
def main(
    dirname: str = typer.Argument(..., help="Directory name"),
    model: str = typer.Option("gpt-3.5-turbo", "--model", "-m", help="Model name"),
    edit_format: str = typer.Option(None, "--edit-format", "-e", help="Edit format"),
    keyword: str = typer.Option(
        None, "--keyword", "-k", help="Only run tests that contain keyword"
    ),
    clean: bool = typer.Option(
        False, "--clean", "-c", help="Discard the current testdir and make a clean copy"
    ),
    no_unit_tests: bool = typer.Option(False, "--no-unit-tests", help="Do not run unit tests"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    stats_only: bool = typer.Option(
        False, "--stats-only", "-s", help="Do not run tests, just collect stats on completed tests"
    ),
    retries: int = typer.Option(2, "--retries", "-r", help="Number of retries for running tests"),
    threads: int = typer.Option(1, "--threads", "-t", help="Number of threads to run in parallel"),
    num_tests: int = typer.Option(-1, "--num-tests", "-n", help="Number of tests to run"),
):
    repo = git.Repo(search_parent_directories=True)
    commit_hash = repo.head.object.hexsha[:7]
    if repo.is_dirty():
        commit_hash += "-dirty"

    dirname = Path(dirname)

    if len(dirname.parts) == 1:
        dirname = BENCHMARK_DNAME / dirname

    now = datetime.datetime.now()
    now = now.strftime("%Y-%m-%d-%H-%M--")

    if not dirname.exists():
        if not re.match(r"\d\d\d\d-\d\d-\d\d-", str(dirname)):
            dirname = dirname.parent / (now + dirname.name)

    dump(dirname)

    if clean and dirname.exists():
        print("Cleaning up and replacing", dirname)
        dir_files = set(fn.name for fn in dirname.glob("*"))
        original_files = set(fn.name for fn in ORIGINAL_DNAME.glob("*"))
        if dir_files != original_files:
            print("ERROR: will not delete dir that does not look like original tests", dirname)
            return

        dest = dirname.parent / "OLD" / dirname.name
        if dest.exists():
            old_now = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            dest = dirname.parent / "OLD" / (old_now + dirname.name)

        dirname.rename(dest)

    if not dirname.exists():
        shutil.copytree(ORIGINAL_DNAME, dirname)

    test_dnames = sorted(os.listdir(dirname))

    if keyword:
        test_dnames = [dn for dn in test_dnames if keyword in dn]

    random.shuffle(test_dnames)
    if num_tests > 0:
        test_dnames = test_dnames[:num_tests]

    if not stats_only:
        build_docker()

    if threads == 1:
        all_results = []
        for testname in test_dnames:
            results = run_test(
                dirname / testname,
                model,
                edit_format,
                retries,
                no_unit_tests,
                verbose,
                commit_hash,
            )

            all_results.append(results)
            summarize_results(dirname)
    else:
        run_test_threaded = lox.thread(threads)(run_test)
        for testname in test_dnames:
            run_test_threaded.scatter(
                dirname / testname,
                model,
                edit_format,
                retries,
                no_unit_tests,
                verbose,
                commit_hash,
            )
        all_results = run_test_threaded.gather(tqdm=True)

    if not stats_only:
        print()
        print()
        print()
    summarize_results(dirname)


def summarize_results(dirname):
    dirname = Path(dirname)
    total_tests = len(list(dirname.glob("*")))
    all_results = [json.loads(fname.read_text()) for fname in dirname.glob("*/.aider.results.json")]

    completed_tests = 0
    try:
        retries = max(len(results["tests_outcomes"]) for results in all_results if results)
    except ValueError:
        retries = 0

    passed_tests = [0] * retries
    duration = 0
    total_cost = 0
    total_error_outputs = 0
    total_user_asks = 0

    variants = defaultdict(set)

    for results in all_results:
        if not results:
            continue

        completed_tests += 1
        passed = results["tests_outcomes"][-1]
        if passed:
            for i in range(len(results["tests_outcomes"]) - 1, retries):
                passed_tests[i] += 1

        total_cost += results["cost"]
        duration += results["duration"]

        total_error_outputs += results.get("num_error_outputs", 0)
        total_user_asks += results.get("num_user_asks", 0)

        for key in "model edit_format commit_hash".split():
            val = results.get(key)
            variants[key].add(val)

    if not completed_tests:
        return

    console = Console(highlight=False)
    console.rule(title=str(dirname))

    console.print(f"test-cases: {completed_tests}")
    for key, val in variants.items():
        if len(val) > 1:
            style = "red"
        else:
            style = None
        val = ", ".join(map(str, val))
        console.print(f"{key}: {val}", style=style)
    print("num_error_outputs:", total_error_outputs)
    print("num_user_asks:", total_user_asks)

    console.print()
    for i in range(retries):
        pass_rate = 100 * passed_tests[i] / completed_tests
        console.print(f"{pass_rate:.1f}% correct after try {i}")

    console.print()
    avg_duration = duration / completed_tests
    remaining_seconds = (total_tests - completed_tests) * avg_duration
    remaining_minutes, remaining_seconds = divmod(remaining_seconds, 60)

    console.print(
        f"duration: {avg_duration:.1f} sec/test-case,"
        f" {remaining_minutes:2.0f}:{remaining_seconds:02.0f} remaining"
    )

    avg_cost = total_cost / completed_tests

    projected_cost = avg_cost * total_tests

    console.print(
        f"costs: ${avg_cost:.4f}/test-case, ${total_cost:.2f} total,"
        f" ${projected_cost:.2f} projected"
    )

    console.rule()


def run_test(testdir, model_name, edit_format, retries, no_unit_tests, verbose, commit_hash):
    if not os.path.isdir(testdir):
        print("Not a dir:", testdir)
        return

    testdir = Path(testdir)

    history_fname = testdir / ".aider.chat.history.md"

    results_fname = testdir / ".aider.results.json"
    if results_fname.exists():
        try:
            res = json.loads(results_fname.read_text())
            return res
        except JSONDecodeError:
            print(f"{results_fname} failed to parse, skipping")
            return

    fnames = []
    for fname in testdir.glob("*"):
        if "test" not in fname.name and fname.is_file() and fname.name[0] != ".":
            fnames.append(fname)

            # restore the original file, in case we interrupted a prev run
            # after it had saved changes
            original_fname = ORIGINAL_DNAME / testdir.name / fname.name
            shutil.copy(original_fname, fname)

    file_list = " ".join(fname.name for fname in fnames)
    intro = testdir / ".docs/introduction.md"
    if intro.exists():
        instructions = intro.read_text() + "\n\n"
    else:
        instructions = ""
    instructions += (testdir / ".docs/instructions.md").read_text()
    instructions += f"""
=====
Use the above instructions to modify the supplied files: {file_list}
Keep and implement the existing function or class stubs, they will be called from unit tests.
Only use standard python libraries, don't suggest installing any packages.
"""

    io = InputOutput(
        pretty=True,
        yes=False,
        chat_history_file=history_fname,
    )

    main_model = models.Model(model_name)
    edit_format = edit_format or main_model.edit_format

    dump(main_model)
    dump(edit_format)
    show_fnames = ",".join(map(str, fnames))
    print("fnames:", show_fnames)

    coder = Coder.create(
        main_model,
        edit_format,
        io,
        os.environ["OPENAI_API_KEY"],
        fnames=fnames,
        use_git=False,
        stream=False,
        pretty=False,
        verbose=verbose,
    )

    dur = 0
    test_outcomes = []
    for i in range(retries):
        start = time.time()
        coder.run(with_message=instructions)
        dur += time.time() - start

        if coder.num_control_c:
            raise KeyboardInterrupt

        if no_unit_tests:
            return

        errors = run_unit_tests(testdir, history_fname)

        if errors:
            test_outcomes.append(False)
        else:
            test_outcomes.append(True)
            break

        errors = errors.splitlines()
        print(errors[-1])
        errors = errors[:50]
        errors = "\n".join(errors)
        instructions = errors
        instructions += (
            f"\n\n####\n\nFix the code in {file_list} to resolve the test failures above."
        )

    results = dict(
        testdir=str(testdir),
        testcase=testdir.name,
        model=main_model.name,
        edit_format=edit_format,
        tests_outcomes=test_outcomes,
        cost=coder.total_cost,
        duration=dur,
        commit_hash=commit_hash,
        num_error_outputs=io.num_error_outputs,
        num_user_asks=io.num_user_asks,
    )
    dump(results)

    results_fname.write_text(json.dumps(results, indent=4))

    return results


def run_unit_tests(testdir, history_fname):
    test_files = [file for file in testdir.glob("*") if file.name.endswith("_test.py")]
    assert len(test_files)

    all_tests_passed = True
    timeout = 60
    for test_file in test_files:
        dump(test_file)

        command = [
            "docker",
            "run",
            "-it",
            "--rm",
            "--interactive=false",
            "-v",
            f"{test_file.parent.absolute()}:/app",
            "benchmark",
            "bash",
            "-c",
            f"pip install pytest && pytest /app/{test_file.name}",
        ]
        print(" ".join(command))

        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                all_tests_passed = False
                print(f"Test {test_file} failed")

            res = result.stdout

        except subprocess.TimeoutExpired:
            all_tests_passed = False
            res = f"Test {test_file} timed out after {timeout} seconds."

        with history_fname.open("a") as fh:
            fh.write(f"```\n{res}\n```")

        if not all_tests_passed:
            return res


def build_docker():
    image_name = "benchmark"
    check_command = ["docker", "images", "-q", image_name]
    check_result = subprocess.run(check_command, stdout=subprocess.PIPE, text=True)
    if check_result.stdout.strip():
        print(f"Docker image '{image_name}' already exists, skipping build.")
        return

    command = [
        "docker",
        "build",
        "--quiet",
        "-t",
        image_name,
        "-f",
        "benchmark/Dockerfile",
        "/dev/null",
    ]
    print(" ".join(command))

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        res = result.stdout
        print(res)

    except subprocess.CalledProcessError as e:
        res = f"Failed to build Docker image: {e.output}"

    return res


if __name__ == "__main__":
    app()