"""Create a new nonsecret workspace and test database for Docker read-only tools."""
import argparse
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='New directory; existing paths are refused')
    root = parser.parse_args().directory.resolve()
    root.mkdir(mode=0o755, parents=False, exist_ok=False)
    (root / 'workspace').mkdir(mode=0o755)
    (root / 'database').mkdir(mode=0o755)
    (root / 'workspace/source.txt').write_text('Research topic: SQLite for small local applications.\n')
    with sqlite3.connect(root / 'database/test.db') as db:
        db.execute('CREATE TABLE articles(title TEXT, topic TEXT)')
        db.executemany('INSERT INTO articles VALUES (?, ?)', [('SQLite overview', 'databases'), ('Python tutorial', 'programming')])
    (root / 'workspace/source.txt').chmod(0o644)
    (root / 'database/test.db').chmod(0o644)
    print(root)


if __name__ == '__main__':
    main()
