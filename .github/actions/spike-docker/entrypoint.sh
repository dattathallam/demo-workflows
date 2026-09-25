#!/bin/sh
set -e

FILE_PATH="${INPUT_FILE_PATH}"
TARGET="${GITHUB_WORKSPACE}/${FILE_PATH}"

if [ -e "$TARGET" ]; then
  INFO=$(stat -c "name=%n size=%s bytes type=%F" "$TARGET" 2>/dev/null || stat -f "name=%N size=%z bytes type=%HT" "$TARGET")
else
  INFO="file not found: ${FILE_PATH}"
fi

echo "File info: ${INFO}"
echo "file-info=${INFO}" >> "$GITHUB_OUTPUT"
