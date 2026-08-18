#!/usr/bin/env bash

restore_file_same_inode() {
  local backup=${1:?backup path required}
  local target=${2:?target path required}
  [ -f "$backup" ] && [ -f "$target" ] || return 1
  cat "$backup" > "$target"
}
