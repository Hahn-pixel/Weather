@echo off
cd /d %~dp0

echo ==============================
echo GIT STATUS
echo ==============================
git status
if errorlevel 1 goto error

echo.
echo ==============================
echo CHECK CHANGES
echo ==============================

git add .

git diff --cached --quiet
if %errorlevel%==0 (
    echo.
    echo [INFO] No changes to commit.
    goto end
)

echo.
echo ==============================
echo GIT COMMIT
echo ==============================
git commit -m "update"
if errorlevel 1 goto error

echo.
echo ==============================
echo GIT PUSH
echo ==============================
git push
if errorlevel 1 goto error

echo.
echo [DONE] Push completed.

goto end

:error
echo.
echo [ERROR] Git command failed.

:end
echo.
pause