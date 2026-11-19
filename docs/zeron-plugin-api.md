# Référence de création des plugins Zeron

Ce guide rassemble les capacités accessibles aux plugins Zeron telles qu’elles sont documentées par le SDK `plugin/zeron.luau` de Graphimo, puis décrit les API spécifiques de Graphimo et d’Orchestrator Builder. Le code source et le manifeste de la version installée restent la référence si le runtime Zeron a évolué.

## Modèle d’un plugin

Un plugin est un dossier signé par Zeron avec un manifeste `zeron-plugin.json`, un fichier d’entrée Luau et une copie du SDK hôte `zeron.luau`. Il peut déclarer des pages, outils pour agents, menus, emplacements UI, service en arrière-plan, réglages et commandes autorisées.

Le fichier d’entrée charge le SDK sous le même identifiant que le manifeste :

```lua
local zeron = require("./zeron")("mon-plugin")

zeron.page {
	id = "home",
	title = "Mon plugin",
	build = function()
		return ui.text { "Bonjour" }
	end,
}
```

Un squelette se crée avec `zeron plugin new`. Après modification, Zeron doit être lancé pour signer avec `zeron plugin sign <id>`. La signature couvre les sources et le SDK présents dans le dossier.

## Manifeste et permissions

Le manifeste donne l’identité et les capacités qui seront présentées à l’utilisateur :

```json
{
  "id": "mon-plugin",
  "name": "Mon plugin",
  "version": "0.1.0",
  "description": "Exemple de plugin",
  "entry": "init.luau",
  "permissions": ["pages.add"],
  "tools": []
}
```

Les permissions sont des capacités, pas des déclarations décoratives : Zeron contrôle les appels et refuse ceux qui ne sont pas accordés. Les noms de commandes autorisées sont également listés dans `commands`.

| Capacité | Permission habituelle |
| --- | --- |
| Page de plugin | `pages.add` |
| Outil agent | `tools` |
| Sessions/projets | `sessions.read` |
| Envoyer/créer un chat, répondre à une question | `messages.send` |
| Événements/messages d’agents | `messages.read` |
| Lire les modèles | `models.read` |
| Modifier le modèle d’un chat ou le défaut | `models.set` |
| Comptes et usage | `accounts.read`, `accounts.switch`, `accounts.pin`, `usage.read` selon l’action |
| Menus et ajouts UI | `ui.add` |
| Remplacer un élément UI | `ui.change` (et autorisation sur le sélecteur visé) |
| Sélecteur/lecture de fichiers | `files`, `files.read` selon l’opération |
| Programme local | `commands`, plus le binaire dans `commands` |
| Navigation interne | `navigate` |
| Liens web | `links.open` |
| GitHub | `github`, écriture selon permission dédiée du runtime |
| Composer | `compose` |
| Brouillons | `draft` |
| Notifications | `notifications` |
| Appareils et messages entre appareils | `devices` |
| Service plugin distant/local | `service` |

Déclare uniquement ce que le plugin utilise. Le runtime et les écrans de consentement de la version installée déterminent les permissions réellement disponibles.

## API `zeron.*`

Les méthodes asynchrones retournent généralement une promesse avec `:after(success, failure)`. Les états UI ont typiquement `:get()`, `:peek()` et `:set(value)`. Les données visibles dépendent des permissions : un champ auquel le plugin n’a pas accès peut simplement être absent.

### Pages, contexte et extension de l’interface

| Méthode | Fonction |
| --- | --- |
| `zeron.page({ id, title, icon?, build })` | Ajoute une page à la barre latérale. `title` peut être une chaîne ou une fonction réactive. Le builder reçoit `{ id, title }`. |
| `zeron.tab(kind, build)` | Enregistre le rendu d’un type d’onglet du plugin dans le panneau d’une session. |
| `zeron.place({ at, before?, after?, replace?, fill? })` | Insère ou remplace un élément UI Zeron par sélecteur/ID/classe/rôle. Le builder est isolé dans son propre composant. |
| `zeron.slot(name, props?)` | Crée un slot auquel d’autres plugins peuvent contribuer du contenu. |
| `zeron.read(name)` | Lit le contexte fourni à un emplacement pendant sa construction ; hors emplacement, seules quelques valeurs globales existent. |
| `zeron.context(name)` | Résout le contexte autorisé et ajoute les champs accessibles aux enregistrements comme session, fichier, message, profil ou panneau. |
| `zeron.need(name)` | Comme `context`, mais masque le composant si la donnée exigée manque et signale la dépendance dans les réglages. |
| `zeron.context_of(name, id)` | Construit le contexte d’un élément donné par ID. |
| `zeron.live(name)` | Dans un emplacement, retourne un lecteur réactif sans reconstruire le composant ni perdre son état. |
| `zeron.menu({ kind, id, label, run })` | Ajoute une entrée à un menu de session/fichier ou à un menu proposé par un autre plugin. |
| `zeron.menu_entries(name, context?)` | Retourne les entrées offertes par d’autres plugins pour un menu du plugin courant. |

Contextes documentés par le SDK : `session`, `sessions`, `message`, `file`, `panel`, `code`, `tex`, `tab`, `page`, `profile`, `appearance`, `route`, `project`, `panel_branch` et `panel_details`. `zeron.context("file")` ne garantit pas un chemin : sans accès de lecture, le plugin ne reçoit qu’un identifiant opaque.

### Outils d’agent, navigation et notifications

| Méthode | Fonction |
| --- | --- |
| `zeron.tool({ name, run })` | Enregistre l’implémentation d’un outil décrit dans `tools` du manifeste. `run(args, call)` peut utiliser `call.chat`. Retourne une valeur ou lève une erreur. |
| `zeron.open(route)` | Va à `"home"`, `{ session = id }` ou `{ settings = page }`. |
| `zeron.open_url(url)` | Ouvre un lien avec le navigateur configuré par l’utilisateur. |
| `zeron.toast(text)` | Affiche une courte notification dans la fenêtre. |
| `zeron.notify({ title, body?, session? })` | Demande une notification système ; le résultat peut être `posted`, `off`, `held`, `paced` ou `unavailable`. |

Le schéma de l’outil dans le manifeste décrit ses arguments et limites ; le code doit néanmoins valider les entrées. Pour les appels lents, effectue le travail de façon asynchrone et retourne une erreur structurée et compréhensible.

### Sessions, modèles, comptes et événements agent

| Méthode | Fonction |
| --- | --- |
| `zeron.sessions()` | Sessions autorisées indexées par ID. |
| `zeron.projects()` | Projets autorisés indexés par ID. |
| `zeron.models(harness, { device? })` | Catalogue des modèles du runtime. |
| `zeron.chat_model(chat)` | Modèle/effort/options d’un chat. |
| `zeron.set_model({ chat, model?, effort?, options? })` | Modifie la configuration du prochain tour. |
| `zeron.set_default_model({ harness, model, effort? })` | Définit le modèle par défaut du runtime. |
| `zeron.new_chat({ prompt, device?, project?, harness?, model?, effort?, cwd? })` | Crée un chat et envoie son premier message. |
| `zeron.send_message({ chat, text, attachments?, mode? })` | Envoie un message ; pendant un tour, `mode` peut mettre en file (`queue`) ou orienter (`steer`) selon le support hôte. Renvoie un reçu. |
| `zeron.answer({ chat, answers, request? })` | Répond à une question/approbation présentée au chat. |
| `zeron.on(event, callback, opts?)` | Écoute les événements et renvoie une fonction de désinscription et une promesse d’inscription. |
| `zeron.reason(err)` | Extrait une catégorie de refus, telle que `offline`, `not_allowed` ou `timeout`. |
| `zeron.usage({ provider?, device? })` / `usage_refresh({ device? })` | Lit l’usage mis en cache ou demande un rafraîchissement. |
| `zeron.accounts({ provider?, device? })` | Liste les comptes connus par Zeron. |
| `zeron.switch_account({ provider, account, when_idle?, device? })` | Change le compte actif. |
| `zeron.pin_account({ provider, chat, account?, device? })` | Épingle un chat à un compte ou retire l’épingle. |
| `zeron.pins({ provider?, device? })` | Liste les épingles de comptes. |

Événements cités par le SDK : `usage.changed`, `usage.threshold`, `usage.reset`, `limit.hit`, `account.switched`, `account.signed_out`, `login.expired`, `model.changed`, `models.updated`, `turn.started`, `turn.finished`, `turn.errored`, `chat.awaiting_input`, `chat.input_answered`. Les options de listener peuvent restreindre le fournisseur, compte, fenêtre ou chat. Attends la promesse d’inscription avant de compter sur le listener.

### GitHub et données de revue

Sous `zeron.github` :

- `pr(url, refresh?)` lit les détails d’une PR ; `diff(url, refresh?, { head?, base? }?)` lit son diff.
- `file_url(url, path)` résout un lien de fichier ; `comment(url, body)` poste un commentaire général.
- `at(session?, project?)` crée les variantes liées à un contexte. Elles incluent `pr`, `diff`, `comment`, `line_comment`, `reply`, `resolve` et `file_url`.
- `repository(project)` déduit le dépôt d’un projet ; `list(project?, repository, opts?)` liste les PR, filtre `authored`/`reviewing`/`all` et prend un curseur/option cache.
- `watch_pr(session)` renvoie un lecteur réactif de la PR de cette session ; `heard()` retourne les PR suivies.

L’authentification reste détenue par Zeron. Les capacités d’écriture sont soumises aux permissions correspondantes. Valide les URLs, chemins, IDs de commit et lignes avant de créer des commentaires.

### Fichiers, commandes, stockage et service

| Méthode | Fonction |
| --- | --- |
| `zeron.run(program, args?, { cwd?, stdin?, timeout?, env? }?)` | Exécute une commande listée dans le manifeste, en passant chaque argument séparément, sans shell. Résultat `{ status, stdout, stderr }`; défaut 60 s, maximum SDK 300 s. |
| `zeron.db.exec(sql, params?)` | Exécute du SQL lié dans la base SQLite du plugin ; résultat `{ changes, last_id }`. |
| `zeron.db.query(sql, params?)` | Lit des lignes SQLite. Utilise toujours les paramètres liés. |
| `zeron.store(key, default)` | Stockage persistant propre au plugin avec `get`, `peek`, `set`. |
| `zeron.devices()` | Appareils accessibles. |
| `zeron.send(device, data)` | Envoie jusqu’à 64 Ko de JSON à un plugin sur un appareil ou `"*"`. |
| `zeron.on_message(fn)` | Écoute les messages inter-appareils reçus. |
| `zeron.service(device?)` | Proxy vers le service du plugin local ou installé sur un autre appareil. |
| `service:call(method, args?, { timeout? }?)` | Appelle une méthode du service ; timeout SDK 30 s par défaut, 300 s max. |
| `service:on(event, fn)` | Écoute les événements du service et renvoie une fonction d’arrêt. |
| `zeron.every(seconds, fn)` | Tâche périodique sans UI, minimum 30 s, fonction d’arrêt retournée. |
| `zeron.window(peek?)` | État `{ shown, active }` de la fenêtre, avec valeurs par défaut sûres. |

Un service est un second fichier Luau, par exemple `service.luau`, déclaré dans le manifeste avec permission `service`. Il s’exécute sans UI sur les appareils où le plugin est activé. Il n’est pas la même chose qu’un outil appelé par un agent.

### Composer, brouillons, réglages et traduction

| Méthode | Fonction |
| --- | --- |
| `zeron.compose(text, { replace?, session? }?)` | Insère du texte dans un brouillon Zeron ; ne l’envoie pas. |
| `zeron.compose_comment({ file, line, note, to?, side?, snippet?, key?, session? })` | Ajoute une puce de commentaire de revue au brouillon. |
| `zeron.composed()` | État des commentaires encore présents dans les brouillons, indexés par clé. |
| `zeron.draft()` | Brouillon courant réactif, texte et types/noms de pièces jointes (pas les chemins). |
| `zeron.draft_of(session)` | Lit le brouillon d’une session. |
| `zeron.drafts()` | Brouillons conservés par clé. |
| `zeron.take_draft(key)` / `put_draft(key)` / `swap_draft(key)` / `drop_draft(key)` | Met de côté, remet, échange ou supprime un brouillon conservé. Limite SDK : 16 brouillons. |
| `zeron.settings()` / `zeron.setting(id)` | Lit les réglages déclarés dans le manifeste. Le plugin ne les écrit pas ; l’utilisateur les change dans Settings. |
| `zeron.on_setting(fn)` | Écoute les changements après initialisation : `(id, value, old_value)`. |
| `zeron.set_status(id, status?)` | Affiche/efface un badge et texte d’état près d’un réglage. |
| `zeron.s(key, args?)` / `zeron.t(key, args?)` | Retourne un texte immédiat ou réactif depuis `locales/`, avec fallback locale parente puis anglais. |

### Globals du runtime UI

Le SDK `zeron.luau` expose les helpers précédents ; le runtime fournit en plus le global `ui` et les composants `@ds/*`. Graphimo utilise notamment `ui.component`, `ui.use_state`, `ui.computed`, `ui.effect`, `ui.task`, `ui.after`, `ui.shared`, `ui.open_files`, `ui.save_file`, `ui.read_file`, `ui.write_file`, `ui.json`, `ui.canvas`, `ui.ref`, `ui.column`, `ui.row`, `ui.div`, `ui.show`, `ui.each`, `ui.presence` et `ui.scroll`. `@ds` inclut des éléments comme `@ds/button`, `@ds/text`, `@ds/dialog`, `@ds/search_field`, `@ds/markdown` et `@ds/glass`.

Cette liste reflète les primitives rencontrées dans les deux plugins étudiés, pas tout l’inventaire de ZGX. Les API UI précises doivent être vérifiées dans le squelette et les packages de la version cible.

## API des deux plugins étudiés

Les appels Zeron ci-dessus sont la couche commune. Les fonctions ci-dessous sont les contrats spécifiques déclarés dans les deux manifestes ; les helpers internes de modules ne deviennent pas des API Zeron par leur seule existence.

### Orchestrator Builder

Le manifeste du Builder déclare l’outil agent `submit_report` avec `nonce` et `report` requis, `result` optionnel `pass|fail`, et jusqu’à quatre findings `{ summary, evidence }`. Le runner corrèle le nonce au chat appelant, enregistre le rapport dans SQLite et n’avance qu’après le tour terminal correspondant.

Le Builder utilise le SDK pour les sessions, modèles, créations/envois de chats, événements, comptes, navigation, Git/GitHub, composer et journal SQLite. La logique métier principale est dans `graph.luau` (modèle et validation), `runner.luau` (runs/journal/routage), `changes.luau` (sources Git/PR, checks, citations) et `init.luau` (interface). Son README contient les limites opérationnelles détaillées.

### Graphimo

Le manifeste Graphimo déclare `pages.add`, `tools`, `commands`, `links.open`, `files`, et autorise `curl` et `python3`. Ses six outils agent sont :

| Outil | Arguments importants | Comportement |
| --- | --- | --- |
| `inspect` | `url?`, `session_id?` | Retourne le notebook, cellules et dépendances marimo actuels. |
| `list_components` | `url?`, `session_id?` | Énumère les callables Python publiques sous `components/`. |
| `add_component` | `component_id`, `callable`, `output_name`, `bindings?` | Insère une cellule Python ordinaire appelant le composant. |
| `connect` | `from_cell_id`, `variable`, `to_cell_id`, `input_ref` | Tente un remplacement source AST étroit, refuse les appels ambigus. |
| `run` | `cell_id` | Demande au serveur marimo d’exécuter la cellule. |
| `open_code` | `cell_id` | Ouvre l’éditeur marimo. |

Les fonctions exportées en interne par `plugin/marimo_bridge.luau` incluent `sessions`, `open_notebook`, `inspect`, `list_components`, `list_components_for_active`, `component_source`, `refresh_component_hashes`, `add_component`, `add_cell`, `run`, `delete`, `connect`, et les helpers de lecture/écriture des logs. Elles ne sont pas toutes exposées comme outils agents.

`plugin/layout.luau` fournit `Layout.load`/`Layout.save` pour `.graphimo.json`, limité aux métadonnées d’affichage. L’UI `plugin/init.luau` utilise les sélecteurs de fichiers Zeron, `zeron.store`, les commandes locales et `zeron.open_url`.

Graphimo parle à une session marimo locale avec `curl` et démarre/inspecte le runtime via `python3`. La frontière marimo est privée et épinglée à `marimo==0.25.2`; marimo demeure propriétaire de l’exécution et du graphe. Le plugin ne demande pas les permissions Zeron pour créer/envoyer des chats ou lire les sessions de chat.

## Limites et sécurité

- `zeron.run` n’est pas un shell, mais exécute tout de même un programme local autorisé. Valide les arguments, le chemin courant et la taille des sorties.
- Les données de fichiers, chats, rapports d’agents, GitHub et services distants sont non fiables. Borne-les et vérifie leur structure.
- Un timeout ne prouve pas qu’une action externe n’a pas eu lieu ; ne renvoie pas une opération non idempotente sans vérifier son état.
- Les permissions restreignent les capacités, mais ne rendent pas un contenu agent digne de confiance.
- `zeron.luau` expose la surface publique de plugin de la copie embarquée. Il ne liste pas toutes les API internes Rust/RPC.
- Les méthodes `ui.*` et composants `@ds/*` évoluent avec Zeron/ZGX et ne sont pas un catalogue stable exhaustif dans ces dépôts.

## Documentation et sources

- [DeepWiki — zeronsh/zeron](https://deepwiki.com/zeronsh/zeron) : architecture du produit (UI, moteur, RPC, agents, sessions, synchronisation), pas un index exhaustif des API plugin.
- [DeepWiki — interface desktop](https://deepwiki.com/zeronsh/zeron/2-desktop-ui-%28zeron-ui-gpui%29) : shell et état UI.
- [DeepWiki — journaux et orchestration des sessions](https://deepwiki.com/zeronsh/zeron/3.3-session-orchestration-and-run-journals) : fonctionnement interne des sessions.
- [DeepWiki — Agent Harnesses](https://deepwiki.com/zeronsh/zeron/4-desktop-ui-%28zeron-ui%29) : abstraction interne des runtimes agents.
- [Architecture Zeron sur GitHub](https://github.com/zeronsh/zeron/blob/main/ARCHITECTURE.md).
- Sources locales : [SDK plugin](../plugin/zeron.luau), [manifeste Graphimo](../plugin/zeron-plugin.json), [bridge marimo](../plugin/marimo_bridge.luau), [Builder README](https://github.com/Master0fFate/zeron-orchestration-builder/blob/initial-release/README.md), et ses modules `graph.luau`, `runner.luau`, `changes.luau`.

La DeepWiki est générée et peut être en retard sur une release. Pour une signature plugin précise, compare le SDK créé par `zeron plugin new`, le manifeste et les permissions de la version de Zeron effectivement installée.
