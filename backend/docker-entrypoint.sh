#!/bin/sh
set -eu

if [ -z "${ROBOTCARE_DATABASE_URL:-}" ]; then
    echo "ROBOTCARE_DATABASE_URL is required" >&2
    exit 64
fi

if [ -z "${ROBOTCARE_JWT_SECRET:-}" ]; then
    echo "ROBOTCARE_JWT_SECRET is required" >&2
    exit 64
fi

echo "Applying database migrations..."
alembic upgrade head

exec "$@"
