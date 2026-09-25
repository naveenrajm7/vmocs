import glob
import re
from pathlib import Path

extensions = ['sphinx.ext.githubpages']

source_suffix = '.rst'
master_doc = 'index'

project = 'vmocs'
copyright = '2026, Naveenraj Muthuraj'
author = 'Naveenraj Muthuraj'
version_text = (Path(__file__).parents[2] / 'lib/vmocs/__init__.py').read_text()
version_match = re.search(r"^__version__ = '([^']+)'$", version_text, re.MULTILINE)
if not version_match:
    raise RuntimeError('cannot find vmocs __version__')
version = version_match.group(1)
release = version

exclude_patterns = []
pygments_style = 'sphinx'
smartquotes = False

html_theme = 'sphinx_rtd_theme'
htmlhelp_basename = 'vmocsdoc'

titles = {
    'vmocs':          'Lightweight VM launcher for SLURM',
    'launch':         'Launch a VM from a template',
    'run':            'Launch a VM and run a guest command',
    'list':           'List running VMs',
    'stop':           'Stop a running VM',
    'template':       'List and inspect VM templates',
    'snapshot':       'Create VM snapshots for fast boot',
    'vmocs.yaml':     'vmocs main configuration file',
    'templates.yaml': 'VM template definitions file',
}

rst_prolog = ''
for page, title in titles.items():
    rst_prolog += '.. |{0}_title| replace:: {1}\n'.format(page, title)

man_pages = []
for f in glob.glob('manpages/*/*.rst'):
    m = re.match(r'(manpages/man(\d)/([\w\-\.]+))\.rst', f)
    if not m:
        continue
    fpath = m.group(1)
    fname = m.group(3)
    section = int(m.group(2))
    title = titles.get(fname, 'vmocs {0}'.format(fname))
    out_name = fname if fname == 'vmocs' else 'vmocs-' + fname
    man_pages.append((fpath, out_name, title, [author], section))
