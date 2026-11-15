# Contributing

## Edit on GitHub

1. Open the file you want to improve and click the pencil icon. GitHub may offer to fork the repository first.
2. Edit the file and choose **Propose changes**. Describe what you changed.
3. Open a pull request against the upstream `main` branch. Include how you tested the change.

## Edit locally

1. Click **Fork** on GitHub and clone your fork.
2. Create a branch with `git switch -c your-change`.
3. Make your change and run:

   ```bash
   python -m py_compile file-f7w.py
   python file-f7w.py --help
   ```

4. If testing browser behavior, use a page and media you have permission to use. Report the OS, Python version, browser and mode, the expected result, and the actual result.
5. Commit and push your branch to your fork, then open a pull request. Keep downloaded media, browser session data, private URLs, and credentials out of commits and issue reports.

GitHub shows merged contributions on the repository's [Contributors page](https://github.com/mahidhar347/browser-media-downloader/graphs/contributors), subject to its attribution rules. Use the Git email associated with your GitHub account, or its GitHub-provided no-reply address.
