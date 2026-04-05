# TaskFlow

TaskFlow is a Flask + MongoDB task management app with:

- one fixed admin account from environment variables
- admin-created users only
- permanent user IDs
- profile editing and password change
- task mentions and notifications
- leaderboard and points

## Local Setup

1. Create and activate a virtual environment.

```bash
python -m venv venv
source venv/bin/activate
```

2. Install dependencies.

```bash
pip install -r requirements.txt
```

3. Copy the example env file and set your real values.

```bash
cp .env.example .env
```

4. Run the app.

```bash
python app.py
```

## Required Environment Variables

Use `.env` locally and Render environment variables in production.

```env
SECRET_KEY=replace-with-a-long-random-secret
MONGO_URI=mongodb+srv://<db-user>:<db-password>@<cluster-url>/taskflow?retryWrites=true&w=majority&appName=TaskFlow
DEFAULT_ADMIN_LOGIN_ID=your-admin-id
DEFAULT_ADMIN_PASSWORD=your-strong-admin-password
DEFAULT_ADMIN_USERNAME=Akay Admin
```

Notes:

- `DEFAULT_ADMIN_LOGIN_ID` is the only account that can keep admin access.
- `DEFAULT_ADMIN_PASSWORD` seeds or refreshes the admin password until that admin changes it personally.
- Once the admin changes their own password, the app stops overwriting it on restart.

## MongoDB Atlas Setup

Use MongoDB Atlas free tier and then copy its SRV connection string into `MONGO_URI`.

Recommended Atlas steps:

1. Create a free Atlas cluster.
2. Create a database user in Atlas.
3. Add your IP or `0.0.0.0/0` temporarily in Network Access.
4. Copy the SRV connection string from Atlas.
5. Replace the placeholders in `MONGO_URI`.

Example:

```env
MONGO_URI=mongodb+srv://atlas-user:atlas-password@cluster0.xxxxx.mongodb.net/taskflow?retryWrites=true&w=majority&appName=TaskFlow
```

Important:

- Replace `atlas-user`, `atlas-password`, and the cluster hostname with your real Atlas values.
- If your password contains special characters like `@` or `#`, URL-encode it before pasting it into the connection string.
- In Atlas Network Access, allowing `0.0.0.0/0` is fine for first deployment, but you should tighten it later if possible.

Official docs:

- Atlas connection strings: https://www.mongodb.com/docs/manual/reference/connection-string-atlas-examples/
- Atlas access and setup: https://www.mongodb.com/docs/atlas/

## Free Deployment Path

This repo is prepared for:

- MongoDB Atlas free cluster
- Render free web service
- GitHub Actions for validation + deploy trigger

### Files Added For Deployment

- `render.yaml`
- `.github/workflows/deploy.yml`
- `.env.example`

### Render Setup

1. Push this repo to GitHub.
2. Create a new Render web service from the repo, or use the `render.yaml` blueprint.
3. In Render, set these environment variables:
   - `SECRET_KEY`
   - `MONGO_URI`
   - `DEFAULT_ADMIN_LOGIN_ID`
   - `DEFAULT_ADMIN_PASSWORD`
   - `DEFAULT_ADMIN_USERNAME`
4. Confirm the start command is:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT
```

5. Copy the Render Deploy Hook URL from your service settings.

Official docs:

- Render docs: https://render.com/docs/
- Render Deploy to Render / blueprint docs: https://render.com/docs/deploy-to-render

### GitHub Actions Setup

This workflow runs on push to `main` and:

- installs dependencies
- compiles `app.py`
- validates Jinja templates
- triggers Render deploy using a deploy hook

Add this GitHub repository secret:

- `RENDER_DEPLOY_HOOK_URL`

Then every push to `main` will:

1. install dependencies
2. compile the Flask app
3. validate Jinja templates
4. trigger a fresh Render deploy

Path:

- `.github/workflows/deploy.yml`

## First Production Login

After deployment, log in with the admin ID and password you set in environment variables:

- admin ID = `DEFAULT_ADMIN_LOGIN_ID`
- admin password = `DEFAULT_ADMIN_PASSWORD`

Then:

1. open the Admin page
2. create user accounts
3. share each user's permanent ID and first password manually

## Security Notes

- `.env` is ignored in `.gitignore`
- admin credentials are no longer hardcoded in `app.py`
- users cannot self-register
- admin can disable or delete users
- admin cannot reset a password after a user has changed it personally
