#!/usr/bin/env bats
# Offline tests for scripts/testenv/summary.sh: component_version formatting against throwaway git
# repos - no cluster, no Docker.

setup() {
    SUMMARY="${BATS_TEST_DIRNAME}/../testenv/summary.sh"
    REPO="${BATS_TEST_TMPDIR}/repo"
    export GVPN_CLIENT_DIR="$REPO" GVPN_SERVER_DIR="$REPO" HOPRD_DIR="$REPO"
}

make_repo() {
    mkdir -p "$REPO"
    git -C "$REPO" init -q -b main
    git -C "$REPO" config user.email t@example.com
    git -C "$REPO" config user.name Test
    echo one >"$REPO/file"
    git -C "$REPO" add file
    git -C "$REPO" commit -q -m first
}

version_line() {
    run bash -c "source '$SUMMARY'; component_version \"\$@\"" _ "$@"
}

@test "a clean checkout on a branch prints branch and short commit" {
    make_repo
    version_line "hoprd" "$REPO"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^\ \ hoprd:\ main\ \([0-9a-f]+\)$ ]]
}

@test "uncommitted changes are marked dirty" {
    make_repo
    echo two >"$REPO/file"
    version_line "hoprd" "$REPO"
    [ "$status" -eq 0 ]
    [[ "$output" == *"(dirty)" ]]
}

@test "a tag on HEAD is printed ahead of the branch" {
    make_repo
    git -C "$REPO" tag v1.2.3
    version_line "hoprd" "$REPO"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^\ \ hoprd:\ v1\.2\.3\ \(main,\ [0-9a-f]+\)$ ]]
}

@test "a detached HEAD is reported as detached" {
    make_repo
    git -C "$REPO" checkout -q --detach HEAD
    version_line "hoprd" "$REPO"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^\ \ hoprd:\ detached\ \([0-9a-f]+\)$ ]]
}

@test "a directory that is not a git checkout is named as such" {
    mkdir -p "${BATS_TEST_TMPDIR}/plain"
    version_line "hoprd" "${BATS_TEST_TMPDIR}/plain"
    [ "$status" -eq 0 ]
    [[ "$output" == *"(not a git checkout)" ]]
}
