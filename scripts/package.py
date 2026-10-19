"""Create a source-only deployment ZIP. Never includes runtime data or credentials."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
out = root / 'dist' / 'sbtg2cloud-source.zip'
out.parent.mkdir(exist_ok=True)
files = [root / name for name in ('Dockerfile', 'compose.yaml', 'requirements.txt', 'README.md',
                                  '.env.example', '.dockerignore', '.gitignore')]
for folder in ('app', 'docs', 'tests', 'scripts'):
    files.extend(p for p in (root / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
with ZipFile(out, 'w', ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, 'sbtg2cloud/' + path.relative_to(root).as_posix())
print(out)
