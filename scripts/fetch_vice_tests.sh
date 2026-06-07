#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${ROOT_DIR}/test/vice"
TMP_DIR="${ROOT_DIR}/.tmp_vice_testprogs"

# Git mirror of upstream VICE sources (contains testprogs/).
VICE_GIT_URL="${VICE_GIT_URL:-https://github.com/VICE-Team/svn-mirror.git}"
VICE_GIT_REF="${VICE_GIT_REF:-master}"
VICE_TESTPROGS_SUBDIR="${VICE_TESTPROGS_SUBDIR:-testprogs}"

if ! command -v git >/dev/null 2>&1; then
  echo "error: git is required to fetch VICE test programs." >&2
  exit 1
fi

echo "Fetching VICE test programs from ${VICE_GIT_URL}@${VICE_GIT_REF}..."
rm -rf "${TMP_DIR}"
mkdir -p "${TMP_DIR}"

git -C "${TMP_DIR}" init -q
git -C "${TMP_DIR}" remote add origin "${VICE_GIT_URL}"
git -C "${TMP_DIR}" config core.sparseCheckout true
printf "%s/\n" "${VICE_TESTPROGS_SUBDIR}" > "${TMP_DIR}/.git/info/sparse-checkout"
git -C "${TMP_DIR}" fetch -q --depth 1 origin "${VICE_GIT_REF}"
git -C "${TMP_DIR}" checkout -q FETCH_HEAD

mkdir -p "${TARGET_DIR}"
rm -rf "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}"
cp -R "${TMP_DIR}/${VICE_TESTPROGS_SUBDIR}/." "${TARGET_DIR}/"

rm -rf "${TMP_DIR}"
echo "VICE test programs downloaded to ${TARGET_DIR}"
