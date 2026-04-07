# TaskFlow

TaskFlow is a Flask + MongoDB task management app with:

- one fixed admin account from environment variables
- admin-created users only
- permanent user IDs
- profile editing and password change
- task mentions and notifications
- leaderboard and points

## Deployment notes

The admin account is controlled by environment variables:

- `DEFAULT_ADMIN_LOGIN_ID`
- `DEFAULT_ADMIN_PASSWORD`
- `DEFAULT_ADMIN_USERNAME`

On Render, these values must match the credentials you use to sign in.

By default, the app now keeps the existing admin password on restart. If you want Render to force-reset the admin password from the environment on the next deploy, set:

- `RESET_ADMIN_PASSWORD_ON_BOOT=1`
