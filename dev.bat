@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
rem ============================================================
rem  tansuo_agent 一键启动（开发模式，前后端各一个独立窗口）
rem    后端：python cli.py web   -> http://127.0.0.1:8000
rem    前端：npm run dev (Vite)  -> http://127.0.0.1:5173
rem          /api 自动代理到后端 :8000（见 web/vite.config.ts）
rem  关闭对应窗口即停止对应服务。
rem  若只需生产模式单端口，直接 python cli.py web 即可，无需本脚本。
rem  端口冲突处理：8000/5173 被占用时，先结束占用进程再启动。
rem  环境变量加载：根目录若存在 .env（KEY=VALUE 格式）会自动加载，
rem    凭据只留在本地（.env 已被 gitignore；模板见 .env.example）。
rem ============================================================
cd /d %~dp0

echo.
echo  tansuo_agent 一键启动
echo    后端  http://127.0.0.1:8000  （cli.py web）
echo    前端  http://127.0.0.1:5173  （Vite 热重载，/api 代理到 :8000）
echo.

rem ---------- .env 加载：把 KEY=VALUE 行解析为环境变量（同名值会被覆盖） ----------
if exist .env (
    echo [提示] 正在加载项目根目录 .env 中的环境变量...
    for /f "usebackq eol=# delims=" %%a in (".env") do (
        set "ENV_LINE=%%a"
        call :set_env
    )
)

if not exist web\node_modules (
    echo [提示] 未发现 web\node_modules，先安装前端依赖，仅首次需要...
    pushd web
    call npm install
    popd
)

rem ---------- 端口占用检测：结束占用进程后再启动 ----------
call :free_port 8000 后端
call :free_port 5173 前端

echo [1/2] 启动后端，窗口名 tansuo-backend ...
start "tansuo-backend" cmd /k python cli.py web

echo [2/2] 启动前端，窗口名 tansuo-frontend ...
start /d web "tansuo-frontend" cmd /k npm run dev

echo.
echo 完成。开发入口：http://127.0.0.1:5173
pause
goto :eof

:free_port
rem %~1=端口号  %~2=用途（仅用于提示文案）
set "FP_FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r ":%~1.*LISTENING"') do (
    if not "%%p"=="0" (
        echo [提示] 端口 %~1（%~2）被进程 %%p 占用，正在结束以腾出端口...
        taskkill /F /PID %%p >nul 2>&1
        set FP_FOUND=1
    )
)
if defined FP_FOUND (
    timeout /t 1 /nobreak >nul
    netstat -ano | findstr /r ":%~1.*LISTENING" >nul
    if not errorlevel 1 echo [警告] 端口 %~1 仍被占用，请手动结束占用进程后重试。
)
goto :eof

:set_env
rem 解析 ENV_LINE 里的一行 KEY=VALUE：去引号、去 export 前缀、去首尾空格；
rem 键为空（如孤立的 =xxx）则跳过。值里的双引号会被整体去除（凭据/URL 不需要引号）。
set "ENV_LINE=!ENV_LINE:"=!"
set "K="
set "V="
for /f "tokens=1,* delims==" %%x in ("!ENV_LINE!") do (
    set "K=%%x"
    set "V=%%y"
)
if "!K:~0,7!"=="export " set "K=!K:~7!"
:trim_k
if "!K:~0,1!"==" " set "K=!K:~1!" & goto trim_k
if "!K:~-1!"==" " set "K=!K:~0,-1!" & goto trim_k
:trim_v
if "!V:~0,1!"==" " set "V=!V:~1!" & goto trim_v
if "!V:~-1!"==" " set "V=!V:~0,-1!" & goto trim_v
if defined K set "%K%=%V%"
goto :eof
