# AirControl AI - final package

site/    -> the website. Deploy this folder to Netlify / Vercel / GitHub Pages.
agent/   -> the program that runs on the laptop you want to control (camera + real cursor).

A website alone cannot move a laptop's cursor (browsers forbid it), so the small agent must run on the
laptop. You never type commands: everything is double-click.

## One-time setup on the laptop
1. Double-click agent\run_windows.bat
   - If Python is missing it installs it (then double-click the file again).
   - First run installs the rest and downloads the hand model (be online).

## Using your deployed website
1. Deploy: drag the "site" folder onto https://app.netlify.com/drop (gives you a link).
2. On the laptop double-click run_windows.bat (leave the window open).
3. Open your Netlify link, sign in, press Start sensing.
4. First time only, Windows asks "Allow this website to control this laptop?" - click Yes.
   (Chrome/Edge may also ask to allow access to devices on your network - click Allow.)

## Make a no-Python .exe (optional)
Double-click agent\build_exe.bat -> dist\AirControlAI.exe. Double-click that instead of run_windows.bat.

## Stop
Stop sensing button | Esc on the dashboard | Ctrl+Alt+Q anywhere | closing the dashboard.

## Gestures
index up = move | thumb+index pinch = click (hold = drag, two quick = double click)
thumb+middle pinch = right click | index+middle up (thumb tucked), move up/down = scroll
index+middle+thumb out, spread/close thumb and index = zoom | fist = pause, open palm = resume
