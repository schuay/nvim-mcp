# Copyright 2026 The showme developers
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import pytest

from showme.clamp import Refused, Root


def test_resolves_a_path_inside_the_root(repo: Path) -> None:
    root = Root.of(repo)
    assert root.resolve("src/main.c") == (repo / "src" / "main.c").resolve()
    assert root.resolve(str(repo / "README.md")) == (repo / "README.md").resolve()


def test_refuses_a_path_outside_the_root(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret"
    outside.write_text("token\n")
    with pytest.raises(Refused, match="outside"):
        Root.of(repo).resolve(str(outside))
    with pytest.raises(Refused, match="outside"):
        Root.of(repo).resolve("../secret")


def test_refuses_a_symlink_that_leaves_the_root(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("token\n")
    (repo / "link.c").symlink_to(tmp_path / "secret")
    with pytest.raises(Refused, match="outside"):
        Root.of(repo).resolve("link.c")


def test_refuses_directories_and_missing_files(repo: Path) -> None:
    with pytest.raises(Refused, match="regular file"):
        Root.of(repo).resolve("src")
    # A path that is not there says so. The two used to share one message, and
    # a client that had the root wrong was told about file types instead.
    with pytest.raises(Refused, match="no such file"):
        Root.of(repo).resolve("nope.c")
    with pytest.raises(Refused, match="no such file"):
        Root.of(repo).resolve("src/nope/deeper.c")


def test_says_outside_before_it_says_missing(repo: Path, tmp_path: Path) -> None:
    # Existence outside the root is not the client's to learn, so the root
    # check runs first for a path that is not there either.
    with pytest.raises(Refused, match="outside"):
        Root.of(repo).resolve(str(tmp_path / "absent"))


def test_does_not_expand_a_tilde(repo: Path) -> None:
    with pytest.raises(Refused):
        Root.of(repo).resolve("~/.ssh/id_ed25519")


def test_refuses_a_root_that_is_not_a_directory(repo: Path) -> None:
    with pytest.raises(Refused, match="not a directory"):
        Root.of(repo / "README.md")
