"""Writes review-kit-guide.html as plain post HTML for WordPress: no stylesheet, fonts or classes,
since a theme supplies its own and WordPress strips <style> from post content for most roles.
Pasted into the code editor without block comments, the body lands as one Classic block.

    python3 docs/wordpress.py [output]     default docs/review-kit-guide.wordpress.html
"""

import os
import re
import sys

here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "review-kit-guide.html"), encoding="utf-8").read()
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "review-kit-guide.wordpress.html")

body = src[src.index('<div class="page">') + len('<div class="page">') : src.rindex("</div>")]
body = re.sub(r"<h1>.*?</h1>\s*", "", body, flags=re.S)  # the post title carries it
body = re.sub(r'<div class="eyebrow">(.*?)</div>', r"<p><em>\1</em></p>", body, flags=re.S)
body = re.sub(r"</?header>\s*", "", body)
body = re.sub(r'<div class="term">(.*?)</div>', r"<pre><code>\1</code></pre>", body, flags=re.S)
body = re.sub(r'<span class="[^"]*">(.*?)</span>', r"\1", body)
body = re.sub(r'<div class="table-wrap">(.*?)</div>', r'<figure class="wp-block-table">\1</figure>', body, flags=re.S)
body = re.sub(r"<footer>(.*?)</footer>", r"<p><em>\1</em></p>", body, flags=re.S)
body = re.sub(r' class="(lede|steps)"', "", body)
# the regexes assume the guide's divs are not nested; any left over means one was cut short
left = body.replace('class="wp-block-table"', "")
if any(t in left for t in ('class="', "style=", "<style", "<link", "<div", "</div")):
    sys.exit("wordpress: markup that depends on the guide's stylesheet remains; update the conversion")

with open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write(body.strip() + "\n")
print(out)
