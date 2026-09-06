# Contributing to JobRadar

JobRadar is an experimental, AI-generated project. Contributions are welcome, including AI-assisted contributions.

## Normal open-source workflow

Users without write access to the upstream repository normally **fork** it, create a branch in their fork, and open a pull request.

```text
upstream JobRadar repository
        ↑ pull request
your fork / feature branch
```

## Commands

```bash
git clone https://github.com/YOUR_USERNAME/JobRadar.git
cd JobRadar

git switch -c feature/my-change

# Make and test changes

git status
git add <files>
git commit -m "Add <feature>"
git push -u origin feature/my-change
```

Then open a Pull Request on GitHub from your branch to the upstream repository.

If you have explicit write access to the upstream repository, you can create the feature branch there instead.

---

## Repository-specific Git identity

If you want a specific GitHub name/email for JobRadar only, set them **without `--global`**:

```bash
git config user.name "YOUR GITHUB DISPLAY NAME"
git config user.email "YOUR_GITHUB_EMAIL_OR_NOREPLY_ADDRESS"
```

Verify:

```bash
git config user.name
git config user.email
```

This configuration applies only to the current repository.

GitHub offers a `noreply` email address if you do not want to expose your normal email in commits.

---

## AI-assisted contributions

AI-assisted changes are welcome, but contributors should test what they submit.

Before sending code to an AI service:

> ⚠️ **Do not include `jobradar.db`.**

Also exclude:

```text
*.db
*.sqlite
.env
API keys
personal AI profiles
private exports
application records
private logs
.venv/
```

Reusable AI prompts and some other examples are available in:

[AI_EXTENSION_PROMPTS.md](AI_EXTENSION_PROMPTS.md)


---

## Suggested Pull Request description

```text
What changed:
- ...

Why:
- ...

How it was tested:
- ...

AI involvement:
- Generated/modified using <tool/model>, if applicable.

Known limitations:
- ...
```

---

## Useful contributions

Examples:

- new Company Watch adapters,
- fixes for existing career-platform variants,
- AI provider integrations,
- routing/geocoding improvements,
- bug fixes,
- diagnostics,
- documentation corrections.

The maintainer is not expected to personally implement every feature request. A tested pull request is therefore especially useful.
