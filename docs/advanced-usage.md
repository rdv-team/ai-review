# Расширенное использование RDV AIR — AI Review

Здесь собраны ручная установка, прямые CLI-команды, пакетная подготовка задач, GitLab MR, MCP, OpenCode и запуск из Jira.

Основной сценарий установки и первой проверки описан в [README](../README.md#быстрый-старт).

## Что создаёт `init`

Успешный ручной запуск из IDE печатает выбранный режим и следующий шаг, например:

```text
MCP: off. Откройте C:\Работа\Ревью\TASK-123 в codex и запустите review-start-task.
Внешняя MCP-проверка отключена; локальная проверка кода доступна.
```

В task capsule создаются, в частности:

```text
TASK-123/
├─ .agents/skills/review-start-task/SKILL.md
├─ .review-tasks/mcp/registry.json
└─ _review_info/
   ├─ meta.txt
   ├─ review_bsl.md
   ├─ review_lines.json
   ├─ changes/
   │  ├─ before/
   │  ├─ after/
   │  └─ open-changes.cmd
   ├─ review_prompt_stage1.md
   └─ review_prompt_stage2.md
```

### Полные изменения во внешнем diff-инструменте

После `init` в `_review_info/changes` доступны полные файлы для сравнения: выделены сохранившиеся изменения задачи или MR, посторонние правки остаются контекстом.

В Windows запустите `_review_info/changes/open-changes.cmd`. По умолчанию нужен установленный KDiff3; также поддерживаются Meld и WinMerge. Каталоги `before` и `after` можно открыть вручную в другом инструменте сравнения.

Чтобы выбрать приложение, добавьте в `.review-tasks.toml` проверяемого репозитория или файл, переданный через `--config`:

```toml
[diff_viewer]
tool = "meld" # kdiff3 | meld | winmerge
executable = '' # пусто — поиск выбранного приложения
```

Для нестандартной установки укажите путь к `.exe`, например `executable = 'C:\Program Files\Meld\Meld.exe'`. После изменения настроек повторите `init`, при необходимости с `--force`.

## Два skill основного маршрута

`review-setup` устанавливает компоненты, получает или клонирует репозиторий проверяемого проекта, проверяет Git и обязательные env, сохраняет prefix config. После него `review-init` получает task/MR и параметры конкретного запуска, запускает `init`, проверяет task capsule и передаёт путь для `review-start-task`.

Сначала:

> Установи и настрой RDV AIR.

После завершённого setup:

> Проверь изменения по задаче `TASK-123`.

Если IDE не обнаружила skills автоматически, подключите [review-setup](../.agents/skills/review-setup/SKILL.md) и [review-init](../.agents/skills/review-init/SKILL.md) явно. Установка через pipx устанавливает CLI, но не добавляет project skills глобально. Ни один из этих skill не отправляет автоматический платный запрос к модели.

## CLI-рецепты

### Одна задача

```powershell
review-tasks init -t "TASK-123" -r "C:\Работа\Продукт 1С" -d "C:\Работа\Ревью" -b "origin/HEAD" --remote origin --ide codex --mcp off
```

Основной сценарий — работа в общей ветке проекта. В примерах используется `origin/HEAD` — ссылка на ветку по умолчанию удалённого репозитория `origin`, обычно `main` или `master`. Это позволяет использовать одно имя для разных проектов. Отдельная ветка задачи не требуется.

Ссылка `origin/HEAD` должна существовать и указывать на нужную общую ветку, содержащую коммиты с ключом задачи. Локальный `HEAD` обозначает текущую позицию рабочей копии и может относиться к другой ветке. Если изменения находятся в другой ветке, передайте её через `-b`: поиск выполняется только в выбранном ref. При другом имени remote используйте `<remote>/HEAD`.

Без `-b` и без сохранённого `branch` CLI использует встроенный порядок выбора: `origin/master → origin/main → master → main → HEAD`. Чтобы использовать именно ветку по умолчанию удалённого репозитория, явно задайте `origin/HEAD` или сохраните его в конфигурации проекта. Для Cursor используйте `--ide cursor`, для OpenCode — `--ide opencode`.

### Несколько задач

Передайте ключи одной строкой через запятую. Ref должен содержать коммиты всех задач:

```powershell
review-tasks init -t "TASK-123,TASK-124" -r "C:\Работа\Продукт 1С" -d "C:\Работа\Ревью" -b "origin/HEAD" --remote origin --ide cursor --mcp off
```

### GitLab Merge Request

Для конкретного MR укажите его ID и базовую ветку. Для поиска MR по задачам используйте `--mr-from-tasks`. Оба режима получают MR refs через Git remote:

```powershell
review-tasks init -t "TASK-123" --mr 42 -r "C:\Работа\Продукт 1С" -d "C:\Работа\Ревью" -b "origin/HEAD" --remote origin --ide codex --mcp off
review-tasks init -t "TASK-123,TASK-124" --mr-from-tasks -r "C:\Работа\Продукт 1С" -d "C:\Работа\Ревью" -b "origin/HEAD" --remote origin --ide codex --mcp off
```

Открывайте конкретные каталоги из вывода CLI: имя task capsule для MR может включать ID MR.

`review-init` также принимает MR URL: из `https://gitlab.example/group/project/-/merge_requests/42` он извлекает ID `42`, проверяет host и namespace/project по выбранному remote и передаёт `--mr 42`. Query и fragment не входят в ID. Сам CLI URL в `--mr` не принимает. Task key остаётся обязательным; при несовпадении проекта или неоднозначном SSH alias skill уточняет вход, не меняя remote. Для этой операции GitLab API token не нужен.

### Дополнительная инструкция агенту

`--user-instruction` принимает Markdown длиной от 1 до 2000 кодовых точек Unicode без NUL. CRLF и CR нормализуются в LF; пустые строки и финальный LF сохраняются.

```powershell
$instruction = @'
После успешного завершения и валидации Stage 1 не выполняй Stage 2 самостоятельно. Запусти отдельного субагента в том же каталоге задачи и поруч ему самостоятельно прочитать `review_prompt_stage2.md` и полностью выполнить Stage 2 по этой инструкции. Дождись завершения субагента. После его успешного завершения заверши свою работу; при ошибке субагента остановись и сообщи об ошибке, не выполняя Stage 2 вместо него.
'@
review-tasks init -t "TASK-123" -r "C:\Работа\Продукт 1С" -d "C:\Работа\Ревью" -b "origin/HEAD" --remote origin --ide codex --mcp off --user-instruction $instruction
```

Значение передаётся одним элементом `argv` и записывается как UTF-8 без BOM в `_review_info/user_instruction.md`. Ввод из файла, stdin, переменной окружения или временного файла не поддерживается.

Не передавайте этим параметром секреты: аргументы командной строки могут быть видны другим локальным процессам. В диагностике local service инструкция заменяется на `<redacted>`.

### Сохранить параметры проекта

Обычный `init` не сохраняет prefix config автоматически. Сначала сохраните параметры префикса:

```powershell
review-tasks prefix-config save TASK "C:\Работа\Продукт 1С" "origin/HEAD" "C:\Работа\Ревью" origin
```

Затем `review-setup` дописывает в эту запись JSON выбранные `ide` (`codex`, `cursor` или `opencode`) и `mcp_enabled` (boolean). Файл: `%USERPROFILE%\.review-tasks\prefix-config.json`, кодировка UTF-8. Например:

```json
{
  "TASK": {
    "repositories": ["C:\\Работа\\Продукт 1С"],
    "branch": "origin/HEAD",
    "reviews_dir": "C:\\Работа\\Ревью",
    "remote": "origin",
    "ide": "codex",
    "mcp_enabled": false
  }
}
```

При записи сохраняйте остальные префиксы. `prefix-config save` перезаписывает выбранную запись; после него выбранные IDE и MCP нужно записать заново. В редакторе проекта Jira те же значения сохраняются через `IDE` и `useMcp`. Отсутствующее `mcp_enabled` в существующей записи означает `true`.

Для ручного запуска skill `review-init` читает сохранённые IDE/MCP и передаёт их в CLI. При прямом вызове CLI задавайте эти flags сами:

```powershell
review-tasks init -t "TASK-123" --ide codex --mcp off
```

Явные параметры запроса к `review-init` имеют приоритет над project defaults. Можно передать любые поддерживаемые `init --help` параметры, в том числе `-r`, `-d`, `-b`, `--remote`, `--ide`, `--mcp`, `--config` и `--result-path`. С полным набором параметров prefix config не обязателен. Для следующей задачи в другой ветке передайте новый `-b`; при отсутствии ключа задачи или других данных конкретного ревью skill уточняет их без повторного setup.

## Установка на Windows

`review-setup` сначала проверяет готовые компоненты. Следующие команды нужны только для отсутствующего компонента и выполняются после согласия пользователя.

### Git и Python

Для типовой установки используйте встроенный Windows Package Manager:

```powershell
winget install --id Git.Git -e --source winget
winget install --id Python.Python.3.11 -e --source winget
```

Закройте и заново откройте терминал или IDE, затем проверьте:

```powershell
python --version
git --version
```

Проект требует Python 3.11 или новее. Если `winget` недоступен, агент после разрешения на установку восстанавливает его официальным способом Microsoft, затем продолжает обычные команды выше. Если App Installer уже установлен, сначала достаточно зарегистрировать его:

```powershell
Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe
winget --version
```

Если App Installer отсутствует, используйте официальный bootstrap из PowerShell с правами администратора:

```powershell
Install-PackageProvider -Name NuGet -Force | Out-Null
Install-Module -Name Microsoft.WinGet.Client -Force -Repository PSGallery | Out-Null
Repair-WinGetPackageManager -AllUsers
winget --version
```

Источник: [установка WinGet у Microsoft](https://learn.microsoft.com/en-us/windows/package-manager/winget/). Не выполняйте bootstrap, если `winget` уже работает. Если нужны права администратора, пользователь подтверждает штатный UAC; после установки обновите терминал/IDE и продолжите с проверки версий Git и Python.

### Установить RDV AIR

RDV AIR устанавливается как Python-пакет `review-tasks` и запускается командой `review-tasks`.

Обычная установка из ZIP не зависит от сохранности распакованной папки. Сама папка понадобится, если вы используете project skills `review-setup`, `review-init` или Chrome extension.

### Установить из Release ZIP

Скачайте ZIP публичной ветки `main` или опубликованной версии и распакуйте его, например в `C:\Работа\review-tasks`.

Проверьте `python -m pipx --version`. Если pipx уже работает, сохраните его версию. Только если pipx отсутствует, установите его для текущего пользователя:

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
```

После `ensurepath` закройте и откройте терминал:

```powershell
Set-Location -LiteralPath "C:\Работа\review-tasks"
python -m pipx install .
review-tasks --help
```

После установки подготовьте задачу командой из раздела [Одна задача](#одна-задача).

### Обновить установку из ZIP

Скачайте новый Release ZIP, распакуйте его в новую папку и выполните:

```powershell
Set-Location -LiteralPath "C:\Работа\review-tasks-новый"
python -m pipx uninstall review-tasks
python -m pipx install .
review-tasks --help
```

`git pull` из ZIP-папки не работает. Старую папку можно убрать после проверки новой установки, сохранив нужные project skill или extension files.

### Установить из Git checkout

Возьмите URL для клонирования с текущей страницы публичного репозитория и выберите ветку `main`:

```powershell
git clone --branch main --single-branch <url-этой-публичной-копии> "C:\Работа\review-tasks"
Set-Location -LiteralPath "C:\Работа\review-tasks"
```

Проверьте `python -m pipx --version`. Только если pipx отсутствует, выполните:

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
```

После `ensurepath` закройте и откройте терминал. Установите CLI из checkout:

```powershell
Set-Location -LiteralPath "C:\Работа\review-tasks"
python -m pipx install -e .
review-tasks --help
```

Editable CLI зависит от сохранности checkout. После `git pull --ff-only` изменения Python-кода доступны без переустановки, пока зависимости не менялись. Если изменился `pyproject.toml`, выполните из checkout:

```powershell
python -m pipx reinstall review-tasks
```

### Установить в venv

Этот вариант нужен, только если пользователь выбрал venv вместо pipx. Executable должен быть доступен в окружении каждого запуска, включая новый workspace и local service:

```powershell
Set-Location -LiteralPath "C:\Работа\review-tasks"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\Activate.ps1
review-tasks --help
```

`python -m review_tasks --help` может помочь диагностике, но не подтверждает готовность. Если работает только он, исправьте PATH или установку entry point по [диагностике](troubleshooting.md#review-tasks-не-находится-в-path). Для обычного запуска из разных IDE/workspaces используйте установку через pipx.

### AI-агент

Устанавливайте только выбранный агент. Для ручного запуска достаточно авторизованной IDE-сессии; запуск из Jira требует соответствующий headless CLI.

Codex CLI для Windows устанавливается официальным PowerShell installer:

```powershell
irm https://chatgpt.com/codex/install.ps1 | iex
codex --version
```

Источник: [официальная документация Codex](https://developers.openai.com/codex/cli/).

Для ручной работы в Cursor установите Windows IDE:

```powershell
winget install --id Anysphere.Cursor -e --source winget
cursor --version
```

`cursor --version` проверяет доступность команды IDE в PATH. Если Cursor открывается и сессия авторизована, отсутствие этой команды не мешает завершить setup: для запуска из Jira сервис использует отдельный executable `agent`.

Официальные Windows installers также доступны на [странице загрузки Cursor](https://prod.cursor.com/en-US/download).

Для запуска из Jira дополнительно установите Cursor Agent CLI:

```powershell
irm 'https://cursor.com/install?win32=true' | iex
agent --version
```

Источник: [официальная документация Cursor](https://prod.cursor.com/help/integrations/cli).

OpenCode CLI на Windows можно установить через npm. Если Node.js отсутствует, сначала установите его:

```powershell
winget install --id OpenJS.NodeJS.LTS -e --source winget
npm i -g opencode-ai@latest
opencode --version
```

Для обновления установленного через npm OpenCode до последней версии выполните:

```powershell
npm i -g opencode-ai@latest
opencode --version
```

Альтернативные способы установки на Windows — Scoop или Chocolatey:

```powershell
scoop install opencode
# или
choco install opencode
```

Источник и способы установки для других ОС: [README OpenCode](https://github.com/anomalyco/opencode#installation).

Вход в Codex, Cursor или OpenCode выполняет пользователь через штатный login flow. Не передавайте агенту password, access token или API key и не проверяйте авторизацию платным prompt.

### Проверить headless CLI

Только для Jira, из обновлённого окружения будущего local service выполните команды для выбранного агента. Если в service config указан `*Bin`, вызывайте именно этот executable. Эти диагностические команды не запускают ревью и не отправляют запрос к модели:

| Агент | Команды | Ожидаемый результат |
| --- | --- | --- |
| Codex | `codex --version`; `codex exec --help`; `codex exec --disable shell_snapshot --help` | Все команды завершаются успешно; CLI поддерживает используемый headless-вызов |
| Cursor | `agent --version`; `agent status` (если отсутствует `CURSOR_API_KEY`); `agent models`; `agent -p --help` | Авторизованная сессия либо env `present`; в help есть `--trust`, `--approve-mcps`, `--force`, `--workspace`; список моделей доступен |
| OpenCode V2 | `opencode --version`; `opencode run --help`; `opencode auth list`; `opencode models` | Команды завершаются успешно; настроена авторизация либо присутствует env выбранного provider; выбранная модель есть в списке |

Команды V2 описаны в [справочнике OpenCode](https://opencode.ai/v2/docs/cli/commands/). Текущий [local service](../src/review_tasks/local_service/orchestrator.py) пока проверяет и передаёт `--command`, `--dir` и один из `--auto` / `--dangerously-skip-permissions`. Эти флаги не описаны в справочнике V2. Успешные проверки из таблицы не означают готовность OpenCode V2 к запуску из Jira: для этого требуется адаптация сервиса. Если установленный CLI не поддерживает эти флаги, setup для Jira с OpenCode завершать нельзя.

Для Cursor сервис использует `cursorModel` из config или `composer-2.5-fast`; если её нет в списке, fallback — `composer-2.5`. Для RouterAI в OpenCode в выводе `opencode models` найдите настроенную модель с префиксом `routerai/`. Версия и help сами по себе авторизацию не подтверждают. Для Codex пользователь завершает штатный login flow; для API keys проверяется только наличие, без проверочного LLM prompt. Отдельной команды общего preflight у `review-tasks` нет: внутренние методы сервиса вызывать из setup не нужно.

## Git-доступ и ветка задачи

### Общий SSH-доступ к GitLab

Эта настройка выполняется один раз во время `review-setup`. Пользователь сам создаёт SSH-ключ и регистрирует его в GitLab; агент только объясняет действия и проверяет результат.

Если доступ уже работает, сохраните его. Если ключа нет, пользователь в своём терминале запускает обычную команду без параметров:

```powershell
ssh-keygen
```

Примите предложенные путь и имя ключа, если они работают. Используйте фактически предложенное имя: оно зависит от версии OpenSSH. Не перезаписывайте существующий ключ. Passphrase пользователь вводит самостоятельно. В GitLab в настройках профиля → SSH Keys добавьте содержимое соответствующего файла `.pub`. Файл без `.pub` — закрытый ключ; его содержимое агент не читает.

```powershell
ssh -T git@<gitlab-host>
```

При первом подключении пользователь сверяет fingerprint с данными администратора GitLab и подтверждает добавление host в `known_hosts`. Если ключ защищён passphrase, пользователь загружает его в `ssh-agent`, совместимый с тем SSH client, который использует Git. Для Windows OpenSSH при необходимости включите службу `ssh-agent` (`Get-Service ssh-agent | Set-Service -StartupType Automatic`, затем `Start-Service ssh-agent`, с правами администратора), после чего пользователь выполняет `ssh-add <путь-к-своему-ключу>` и вводит passphrase вне chat. Не смешивайте ssh-agent из Git Bash и Windows OpenSSH.

Источник: [управление ключами Windows OpenSSH](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement).

### Если SSH не работает с кириллицей в пути

Применяйте этот обход только при ошибке чтения/создания SSH-файлов в профиле с кириллицей. Создайте `C:\git-ssh-home\.ssh` для текущего пользователя, без предоставления доступа к private keys другим пользователям. `ssh-keygen` по-прежнему запускается без параметров; в ответ на запрос имени файла пользователь вводит `C:\git-ssh-home\.ssh\<предложенное-имя-ключа>` (например, `id_ed25519` или `id_rsa`). Если ключ уже создан, пользователь может сам скопировать пару в этот каталог, сохранив ограничения доступа, вместо создания нового ключа.

Агент создаёт UTF-8 файл `C:\git-ssh-home\.ssh\config` с конкретными host, SSH user/port из URL и фактическим именем ключа. Например, для `id_ed25519`:

```sshconfig
Host gitlab.example
    HostName gitlab.example
    User git
    IdentityFile C:/git-ssh-home/.ssh/id_ed25519
    IdentitiesOnly yes
    UserKnownHostsFile C:/git-ssh-home/.ssh/known_hosts
```

Для нестандартного порта добавьте `Port <порт>`. Существующий config меняйте точечно, сохраняя остальные hosts. Пользователь регистрирует public key и проверяет fingerprint как в обычном сценарии. Проверка теперь использует этот config:

```powershell
ssh -F C:/git-ssh-home/.ssh/config -T git@gitlab.example
```

Для нового репозитория сохраните SSH-команду при клонировании:

```powershell
git -c 'core.sshCommand=ssh -F C:/git-ssh-home/.ssh/config' clone --config 'core.sshCommand=ssh -F C:/git-ssh-home/.ssh/config' <git-url> <каталог-клонирования>
```

Для уже существующего репозитория:

```powershell
git -C "C:\Работа\Продукт 1С" config --local core.sshCommand 'ssh -F C:/git-ssh-home/.ssh/config'
```

Если нужен определённый SSH executable, замените `ssh` его абсолютным путём с корректными кавычками и используйте тот же client при проверке. Существующую нестандартную SSH-команду сохраняйте и дополняйте, не отбрасывая её параметры. `GIT_SSH_COMMAND` в окружении имеет приоритет над `core.sshCommand`: при наличии согласуйте его с выбранным config в окружении запуска. Так путь хранится в локальном Git config, и clone/fetch/service используют один SSH config. Перемещать профиль Windows или менять `HOME`/`USERPROFILE` не требуется.

Основание: [Git core.sshCommand](https://git-scm.com/docs/git-config#Documentation/git-config.txt-coresshCommand) и [параметры OpenSSH config](https://man.openbsd.org/ssh_config).

После ручной настройки `review-setup` повторно проверяет SSH-аутентификацию. Успешная проверка подтверждает вход в GitLab, но не права на конкретный репозиторий.

### Remote и ветка задачи

Эти проверки выполняет `review-setup`. Сначала пользователь завершает интерактивную авторизацию GCM/SSH и подтверждение host key. Затем агент запускает `ls-remote` через process API с timeout 60 секунд и временным окружением только дочернего процесса. Пример Python, где `repo` и `remote` берутся из согласованных параметров:

```python
import os
import subprocess

repo = r"C:\Работа\Продукт 1С"
remote = "origin"
env = os.environ.copy()
env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
           SSH_ASKPASS_REQUIRE="never")
configured = subprocess.run(
    ["git", "-C", repo, "config", "--get", "core.sshCommand"],
    capture_output=True, text=True, timeout=10,
)
ssh_command = env.get("GIT_SSH_COMMAND") or configured.stdout.strip() or env.get("GIT_SSH") or "ssh"
env["GIT_SSH_COMMAND"] = ssh_command + " -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15"
try:
    result = subprocess.run(
        ["git", "-C", repo, "ls-remote", "--exit-code", remote, "HEAD"],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, timeout=60,
    )
    print("Git access:", "ready" if result.returncode == 0 else "failed")
except subprocess.TimeoutExpired:
    print("Git access: timeout")
```

SSH flags в примере предназначены для OpenSSH. Если настроен другой SSH client, сохраните его и используйте его non-interactive flags с тем же timeout. Ожидается exit code 0; `failed` или `timeout` требуют исправления доступа, а не обхода проверки host key. Не выводите env или необработанный stderr с credential. После успешной проверки обновите refs и проверьте выбранный ref:

```powershell
git -C "C:\Работа\Продукт 1С" remote get-url origin
git -C "C:\Работа\Продукт 1С" fetch origin --prune
git -C "C:\Работа\Продукт 1С" rev-parse --verify "origin/HEAD"
```

Проверьте, на какую ветку указывает ссылка:

```powershell
git -C "C:\Работа\Продукт 1С" symbolic-ref refs/remotes/origin/HEAD
```

Ожидается ссылка на общую ветку проекта, например `refs/remotes/origin/main` или `refs/remotes/origin/master`. Если `origin/HEAD` отсутствует или устарел, его можно обновить по текущей ветке по умолчанию удалённого репозитория:

```powershell
git -C "C:\Работа\Продукт 1С" remote set-head origin --auto
```

Затем повторите проверку ссылки и `rev-parse --verify`. Если общая ветка проекта отличается от ветки по умолчанию, укажите её явно, например `origin/main` или `origin/master`. Не меняйте remote ради совпадения с примерами документации.

## Подключить MCP

Режим `required` требует совместимых MCP endpoints и прав доступа. Точные URL paths и ожидаемые tools заданы в [packaged registry](../src/review_tasks/runtime/task/mcp/registry.json). Получите действующие host и token у владельца сервисов; placeholders не являются работающими MCP-серверами.

| Переменная | Назначение |
| --- | --- |
| `RDV_MCP_1C_URL` | Имя хоста без схемы и завершающего `/`; URL строится как `https://<host>/<path>` |
| `RDV_MCP_1C_TOKEN` | Bearer-токен MCP-серверов |

Секреты задавайте самостоятельно в параметрах системы или текущей сессии PowerShell. Не передавайте их агенту и не сохраняйте в примерах. `review-setup` проверяет только наличие нужных env в окружении запуска. Доступность endpoints и expected tools до создания task capsule не является условием завершения setup; работа MCP проверяется при ревью подготовленной задачи.

При изменении User/System env закройте и заново откройте IDE/терминал, запускающий агента или local service, затем перезапустите сам сервис. Перезапуск дочернего процесса из старой IDE может сохранить старое окружение. Переменная, заданная только в отдельной PowerShell session, доступна только этой session и её дочерним процессам.

## Настроить OpenCode и RouterAI

Этот раздел описывает OpenCode V2 и нужен при необходимости настроить provider. При готовой авторизации сохраните текущие provider и model settings.

Установите OpenCode по разделу [AI-агент](#ai-агент).

Глобальные настройки находятся в `%USERPROFILE%\.config\opencode`. OpenCode поддерживает `opencode.json` и `opencode.jsonc`; см. [официальную документацию V2](https://opencode.ai/v2/docs/config/). Используйте один основной файл: существующий `opencode.jsonc`, иначе существующий `opencode.json`, иначе новый `opencode.jsonc`. Если оба файла уже есть, сначала сделайте резервные копии и сведите настройки в один файл, не полагаясь на порядок загрузки.

Файл `opencode.json`, который появляется в task capsule при `init --ide opencode --mcp required`, относится только к этой задаче. При `--mcp off` MCP connection config не создаётся.

Пример для V2 по [документации providers](https://opencode.ai/v2/docs/providers/). Файл `.agents/skills/review-setup/references/opencode-routerai.example.jsonc` пока содержит формат V1; для V2 используйте конфигурацию ниже:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "model": "routerai/deepseek/deepseek-chat-v3.1",
  "providers": {
    "routerai": {
      "package": "@opencode/ai/providers/openai-compatible",
      "name": "RouterAI",
      "settings": {
        "baseURL": "https://routerai.ru/api/v1",
        "apiKey": "{env:ROUTERAI_API_KEY}"
      },
      "models": {
        "deepseek/deepseek-chat-v3.1": {
          "name": "DeepSeek V3.1"
        }
      }
    }
  }
}
```

`{env:ROUTERAI_API_KEY}` ссылается на переменную окружения и не содержит токен. Проверка ниже не отправляет запрос к модели:

```powershell
opencode models
```

В списке должна быть модель `routerai/deepseek/deepseek-chat-v3.1`. Её наличие подтверждает загрузку настройки, но не успешность запросов к API.

Не запускайте автоматический пробный запрос к модели при проверке настройки.

## Запускать ревью из Jira

Этот маршрут нужен, чтобы запускать подготовку со страницы Jira RDV. Он требует local service, Chrome/Chromium 102+ и headless CLI выбранного агента: `codex`, `agent` (Cursor Agent CLI) или `opencode`.

### 1. Настроить local service

Для автоматической настройки агент использует `review-tasks local-service config`. Настройки хранятся в `%USERPROFILE%\.review-tasks\local-service\service.json`. Сначала проверьте существующие настройки через `--show`. Существующий локальный extension token сохраняйте; только при его отсутствии создайте новый:

```powershell
$localExtensionToken = [guid]::NewGuid().ToString('N')
review-tasks local-service config --host 127.0.0.1 --port 8765 --token $localExtensionToken
Write-Output $localExtensionToken
```

Этот локальный token разрешено показать пользователю для переноса в popup. Он не связан с внешними ключами RouterAI, MCP и GitLab, которые выводить нельзя. Если host/port уже настроены и работают, сохраните их вместо defaults из примера.

При необходимости задайте путь только выбранного агента через `--codex-bin`, `--cursor-bin` или `--opencode-bin`; если executable доступен по PATH и нет конфликтующего override, дополнительные пути не нужны. `--cursor-model` относится только к Cursor. Выполните [проверки выбранного CLI](#проверить-headless-cli).

GitLab API требуется только для поиска MR в Jira extension. Для TASK этот шаг пропустите. При выборе MR discovery запросите GitLab base URL, API base URL (обычно `<baseUrl>/api/v4`) и имя env, например `RDV_GITLAB_API_TOKEN_REVIEW`:

```powershell
review-tasks local-service config --gitlab-instance https://gitlab.example https://gitlab.example/api/v4 RDV_GITLAB_API_TOKEN_REVIEW
```

Для нескольких instances передайте флаг для каждого в одной команде: список instances заменяется целиком. Пользователь сам создаёт GitLab token с доступом к нужным проектам и чтением API (`read_api` для обычного PAT), затем задаёт его в указанной env. Агент проверяет только наличие. См. [создание GitLab token](https://docs.gitlab.com/user/profile/personal_access_tokens/).

После обновления окружения запустите `review-tasks local-service` либо `start-jira-local-review-service.bat`. Окно самостоятельного запуска сервиса должно оставаться открытым. `config-jira-local-review-service.bat` — альтернативный интерактивный мастер для пользователя; агенту не нужно проходить его вопросы об остальных агентах.

Проверьте настройки и состояние сервиса; команда `--show` скрывает значение токена:

```powershell
review-tasks local-service config --show
Invoke-RestMethod http://127.0.0.1:8765/health
```

Используйте фактический host/port из effective config. Ожидается HTTP 200, `ok: true` и `service: review-tasks-local-review`. Это проверка работающего сервиса; готовность headless CLI проверяется отдельными командами выше.

Для остановки нажмите `Ctrl+C` в окне сервиса.

### 2. Подключить Chrome extension

После успешного `/health`:

1. откройте `chrome://extensions`;
2. включите **Режим разработчика**;
3. нажмите **Загрузить распакованное расширение**;
4. выберите `extension/jira-local-review-extension` из корня checkout RDV AIR;
5. на вкладке **Локальный сервис** задайте port из `service.json` и локальный extension token.

Ключи RouterAI, MCP и GitLab в extension вводить нельзя. После изменения файлов расширения нажимайте **Обновить** у распакованного расширения.

### 3. Настроить префикс Jira и запустить ревью

На вкладке **Конфигурация проекта** задайте для префикса, например `RM`:

- `IDE`: Codex, Cursor или OpenCode;
- `repoPath`: путь к Git-репозиторию продукта, по одному на строку;
- `reviewsRoot`: корневой каталог task capsules;
- `branch`: проверенный ref, например `origin/HEAD`;
- `remote`: имя remote, обычно `origin`;
- `useMcp`: включена ли внешняя MCP-проверка проекта.

Конфигурация хранится в `%USERPROFILE%\.review-tasks\prefix-config.json`. API `PUT /prefix-config/{prefix}` принимает `useMcp`, а в файле хранится boolean `mcp_enabled`. `true` соответствует `--mcp required`, `false` — `--mcp off`; отсутствующее поле означает `true`. Сервис читает сохранённый выбор и явно передаёт режим в CLI. При `off` не нужны MCP env и IDE-specific MCP connection files; остальные проверки задачи и агента остаются. Отдельного MCP override в popup или `POST /reviews` нет.

После сохранения прочитайте конфигурацию через extension: это проверяет связь с сервисом и локальный token без запуска ревью. Если параметры уже сохранил агент, повторно вводить их не нужно. Когда задача получит статус **Готово к ревью**, выберите в extension источник `TASK` или `MR`, IDE и запустите ревью.

Поле **Инструкция агенту** хранится в `chrome.storage.local` отдельно для каждого ключа задачи. Лимит — 2000 символов. Не вводите в него секреты.
