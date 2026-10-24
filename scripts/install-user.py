#!/usr/bin/env python3
"""Install this application's user-local entry and licensed fonts."""
from pathlib import Path
import shlex
import shutil
import subprocess

root=Path(__file__).resolve().parents[1]
home=Path.home()
name='thd-markdown-web'
launcher=home/'.local/bin'/name
launcher.parent.mkdir(parents=True,exist_ok=True)
launcher.write_text('#!/usr/bin/env bash\nexec '+shlex.quote(str(root/'scripts/run.sh'))+' "$@"\n')
launcher.chmod(0o755)
fonts=home/'.local/share/fonts'/name
fonts.mkdir(parents=True,exist_ok=True)
for font in (root/'data/fonts').iterdir(): shutil.copy2(font,fonts/font.name)
subprocess.run(['fc-cache',str(fonts)],check=True)
applications=home/'.local/share/applications'
applications.mkdir(parents=True,exist_ok=True)
executable=str(root/'scripts/run.sh').replace('\\','\\\\').replace('"','\\"').replace('`','\\`').replace('$','\\$')
desktop=applications/('io.github.the_hidden_door_llc.thd_markdown.web'+'.desktop')
desktop.write_text('[Desktop Entry]\nType=Application\nName='+'THD Markdown Web'+'\nExec="'+executable+'" %U\nIcon='+str(root/'data/thd-markdown.svg')+'\nTerminal=false\nCategories='+'Network;WebBrowser;GTK;'+'\nStartupNotify=true\nStartupWMClass='+'io.github.the_hidden_door_llc.thd_markdown.web'+'\n')
subprocess.run(['desktop-file-validate',str(desktop)],check=True)
subprocess.run(['update-desktop-database',str(applications)],check=True)
print('Installed '+name+' for the current user.')
