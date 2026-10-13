import subprocess

res = subprocess.run(['git', 'show', 'c72c5fe:avatar.html'], capture_output=True)
c = res.stdout.decode('utf-8', errors='ignore')
with open('avatar.html', 'r', encoding='utf-8') as f:
    w = f.read()

s_tag = '<script type="module">'
e_tag = '</script>'

c_s = c.find(s_tag)
c_e = c.find(e_tag, c_s)
w_s = w.find(s_tag)
w_e = w.find(e_tag, w_s)

c_mod = c[c_s:c_e]
w_mod = w[w_s:w_e]

print('c_mod len:', len(c_mod))
print('w_mod len:', len(w_mod))
print('Equal module script?', c_mod == w_mod)

import difflib
diff = list(difflib.unified_diff(c_mod.splitlines(), w_mod.splitlines(), lineterm=''))
print('Module script diff lines:', len(diff))
for line in diff:
    if line.startswith('+') or line.startswith('-'):
        print(line[:100])
