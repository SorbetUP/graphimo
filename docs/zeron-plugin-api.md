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

### Règles de manifeste à vérifier

- `id` est l’identifiant stable du plugin et doit être exactement celui passé à `require("./zeron")(id)`.
- `entry` doit désigner un fichier présent dans le paquet. Les chemins de `icon`, `service`, les locales et les autres ressources doivent eux aussi être présents et respecter la casse.
- Chaque outil doit être déclaré une seule fois dans `tools`, avec un schéma JSON cohérent, puis enregistré par `zeron.tool` avec le même nom.
- Pour une commande, déclare `commands` dans les permissions et le binaire dans le tableau `commands`. Passe le binaire et ses arguments séparément à `zeron.run`; ne compose jamais une ligne shell.
- `settings` est une déclaration de schéma et de valeurs par défaut. Le plugin lit les valeurs avec `zeron.settings()` / `zeron.setting(id)`; l’utilisateur les modifie dans Zeron.
- La version du manifeste, les sources, les ressources et la copie de `zeron.luau` doivent être celles que tu veux signer. Toute modification après signature impose une nouvelle signature.

Le manifeste exact accepté peut évoluer. Pour une capacité ou un champ incertain, inspecte le squelette généré par la version de Zeron installée plutôt que d’inventer un champ.

## API `zeron.*`

Les méthodes asynchrones retournent généralement une promesse avec `:after(success, failure)`. Les états UI ont typiquement `:get()`, `:peek()` et `:set(value)`. Les données visibles dépendent des permissions : un champ auquel le plugin n’a pas accès peut simplement être absent.

L’inventaire des noms publics ci-dessous a été comparé aux deux copies locales de `zeron.luau` : `plugin/zeron.luau` de Graphimo et `zeron.luau` d’Orchestrator Builder. Les deux copies exposent la même famille de méthodes `zeron.*` documentée ici, mais leurs implémentations ne sont pas identiques. Les helpers internes et le runtime UI peuvent donc différer. Cette référence décrit la surface observée, pas une promesse de compatibilité entre versions.

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

Le service a aussi un environnement d’exécution distinct de la page. Les commentaires du SDK énumèrent pour cette entrée des helpers de runtime comme `zeron.serve`, `zeron.tool`, `zeron.every`, `zeron.on_stop`, `zeron.emit`, `zeron.run`, `zeron.fetch`, `zeron.notify`, `zeron.sleep`, `zeron.task`, `zeron.store`, `zeron.json` et `zeron.device`. La copie UI de `zeron.luau` ne spécifie pas leurs signatures. Pour les employer, pars du `service.luau` ou de l’exemple généré par la version cible; ne transpose pas les contrats de la page et ne déduis pas une signature depuis les noms.

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

### Pavé tactile et interactions de canvas

Les gestes d’un canvas sont gérés par ses callbacks de pointeur, de molette et de pincement. Ils ne font pas partie de l’API publique `zeron.*` : les noms et champs d’événement ci-dessous sont ceux observés dans le runtime Zeron et dans Graphimo. Vérifie-les dans le `ui.canvas` de la version cible avant de les réutiliser.

| Geste / callback | Comportement recommandé |
| --- | --- |
| `on_press(e)` | Mémoriser la position initiale. Si un élément est sous le pointeur, le sélectionner et préparer son déplacement; sinon, préparer le déplacement du canvas. |
| `on_pointer_move(e)` | Pendant un déplacement actif, déplacer l’élément sélectionné ou translater le canvas avec le delta de pointeur. |
| `on_release(e)` | Terminer le déplacement et nettoyer l’état temporaire, y compris si le geste s’est achevé sans mouvement. |
| `on_wheel(e)` avec `e.cmd` ou `e.ctrl` | Zoomer autour de `e.local_x`, `e.local_y`, en tenant ce point fixe dans le canvas. `e.dy` fournit le sens et l’amplitude du zoom. |
| `on_wheel(e)` sans modificateur | Translation du canvas avec `e.dx` et `e.dy`; cela prend en charge le défilement à deux doigts du pavé tactile. |
| `on_pinch(e)` | Zoomer autour du point du geste avec `e.scale` et `e.local_x`/`e.local_y` (ou `e.x`/`e.y` si la version du runtime ne fournit pas les coordonnées locales). |

Exemple de base pour un canvas dont le zoom est centré sur le curseur ou le centre du pincement. `zoom_at` doit ajuster l’échelle et la translation ensemble afin que le point situé sous le geste reste immobile à l’écran; borne l’échelle dans cette fonction.

```lua
local function wheel(e)
    if e.cmd or e.ctrl then
        local factor = math.exp(-e.dy * 0.01)
        zoom_at(e.local_x, e.local_y, factor)
    else
        pan_x = pan_x - (e.dx or 0)
        pan_y = pan_y - (e.dy or 0)
    end
    return true
end

local function pinch(e)
    local x = e.local_x or e.x
    local y = e.local_y or e.y
    zoom_at(x, y, 1 + e.scale)
    return true
end
```

Pour la sélection et le déplacement, conserve un état de geste explicite (`idle`, `pan`, `drag`) : au `on_press`, fais le hit-test et sélectionne la cible; sur fond vide, passe en `pan`; pendant `on_pointer_move`, applique le delta; au `on_release`, remets l’état à `idle`. Ne déclenche pas le déplacement d’un bloc si le pointeur est relâché avant le seuil de mouvement prévu par l’interface.

Conseils de compatibilité :

- Le scroll à deux doigts arrive par `on_wheel`; le pincement peut arriver séparément par `on_pinch`. Implémente les deux pour couvrir les pavés tactiles qui émettent l’un ou l’autre.
- `e.scale` est un delta relatif dans l’implémentation observée : l’exemple l’emploie comme facteur `1 + e.scale`. Vérifie le signe et l’échelle dans le runtime installé avant de modifier cette formule.
- Les événements peuvent fournir `dx`/`dy`, les coordonnées locales, les modificateurs et un delta de pointeur différents selon la version. Gère les valeurs absentes et évite de traiter le même geste deux fois si l’hôte émet plusieurs événements.
- Retourne `true` quand le callback consomme le geste, selon le contrat du composant utilisé; vérifie ce contrat dans la version Zeron visée.
- Utilise toujours le repère local du canvas pour les coordonnées de zoom et de hit-test. Nettoie l’état du geste sur relâchement et lors de la destruction du composant.

La combinaison utilisée dans Graphimo est la suivante : clic sur un bloc pour le sélectionner, glissement d’un bloc pour le déplacer, glissement du fond pour déplacer la vue, défilement à deux doigts pour déplacer la vue, `Ctrl`/`Cmd` + défilement pour zoomer, et pincement à deux doigts pour zoomer. Zeron utilise aussi `on_pinch` et les deltas de molette dans ses vues internes, par exemple dans le [visualiseur de fichiers](https://github.com/zeronsh/zeron/blob/main/apps/zgx/ui/ds/file_viewer.luau) et la [lightbox](https://github.com/zeronsh/zeron/blob/main/apps/zgx/ui/ds/lightbox.luau); ces exemples sont des indices de compatibilité, pas une garantie de stabilité publique.

## API des deux plugins étudiés

Les appels Zeron ci-dessus sont la couche commune. Les fonctions ci-dessous sont les contrats spécifiques déclarés dans les deux manifestes ; les helpers internes de modules ne deviennent pas des API Zeron par leur seule existence.

### Orchestrator Builder

Le manifeste du Builder déclare l’outil agent `submit_report` avec `nonce` et `report` requis, `result` optionnel `pass|fail`, et jusqu’à quatre findings `{ summary, evidence }`. Le runner corrèle le nonce au chat appelant, enregistre le rapport dans SQLite et n’avance qu’après le tour terminal correspondant.

Le Builder utilise le SDK pour les sessions, modèles, créations/envois de chats, événements, comptes, navigation, Git/GitHub, composer et journal SQLite. La logique métier principale est dans `graph.luau` (modèle et validation), `runner.luau` (runs/journal/routage), `changes.luau` (sources Git/PR, checks, citations) et `init.luau` (interface). Son README contient les limites opérationnelles détaillées.

Le runner est un exemple avancé, pas un squelette à recopier pour tout plugin : il associe les rapports d’outils à un nonce d’étape, au chat appelant, aux événements de tour et aux reçus de livraison, puis bloque quand le résultat est incertain. Les runs distants ne sont pas pris en charge dans son README; l’arrêt n’annule ni un tour déjà parti ni un message mis en file. Réutilise les principes de corrélation, de journal et de non-renvoi après timeout seulement si ton plugin orchestre réellement des effets asynchrones comparables.

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

## Démarrage rapide reproductible

Commence dans un dossier de développement séparé du dépôt source, avec Zeron installé et lancé :

```sh
zeron plugin new
```

Garde le manifeste et `zeron.luau` générés comme base. Remplace les fichiers d’entrée et ajoute seulement les ressources dont le plugin a besoin. Le squelette de la version installée est la référence pour les champs encore absents ou différents de cet exemple minimal :

```json
{
  "id": "hello-zeron",
  "name": "Hello Zeron",
  "version": "0.1.0",
  "description": "Une page de démonstration.",
  "entry": "init.luau",
  "permissions": ["pages.add"],
  "tools": []
}
```

```lua
local zeron = require("./zeron")("hello-zeron")
local Button = require("@ds/button")

zeron.page {
    id = "home",
    title = "Hello Zeron",
    build = function()
        return ui.column {
            ui.text { "Plugin chargé." },
            Button {
                "Vérifier",
                on_click = function()
                    zeron.toast("La page répond.")
                end,
            },
        }
    end,
}
```

Le but du premier passage est de vérifier le chargement de la page et la cohérence de l’identifiant. Ajoute ensuite une fonctionnalité à la fois, en ne demandant sa permission qu’au moment où elle est utilisée. Pour un outil agent, vérifie le schéma et l’implémentation ensemble :

```json
{
  "permissions": ["pages.add", "tools"],
  "tools": [{
    "name": "echo_text",
    "description": "Retourne le texte reçu.",
    "input": {
      "type": "object",
      "properties": { "text": { "type": "string", "maxLength": 2000 } },
      "required": ["text"],
      "additionalProperties": false
    }
  }]
}
```

```lua
zeron.tool {
    name = "echo_text",
    run = function(args, call)
        if type(args) ~= "table" or type(args.text) ~= "string" then
            error("text doit être une chaîne")
        end
        if #args.text > 2000 then
            error("text dépasse 2000 caractères")
        end
        return { text = args.text, caller = call and call.chat or nil }
    end,
}
```

La limite est validée dans le code même si elle figure dans le schéma : les agents, fichiers et réponses d’outils restent des entrées non fiables. Si l’outil appelle une API asynchrone, ajoute le chemin de succès et le chemin d’échec avec `:after(success, failure)` et renvoie un résultat ou une erreur compréhensible.

## Cycle de développement et de distribution

1. **Générer** avec `zeron plugin new`; ne pars pas d’un manifeste supposé être universel.
2. **Décrire le contrat** avant le code : données d’entrée, sortie, effets de bord, permissions, traitement des erreurs, limites de taille et comportement en cas de timeout.
3. **Implémenter par couches** : validation métier, adaptation Zeron, puis UI. Garde les opérations longues hors des callbacks UI et borne les données conservées.
4. **Comparer manifeste et code** : identifiant du SDK, fichiers présents, outils enregistrés, permissions utilisées, commandes autorisées et ressources référencées.
5. **Vérifier dans Zeron** : installer ou ouvrir le plugin, approuver uniquement les capacités nécessaires, ouvrir chaque page, essayer les chemins valides et invalides, désactiver une permission pour confirmer que le refus est propre, puis recharger le plugin.
6. **Signer après les changements** avec Zeron lancé :

   ```sh
   zeron plugin sign hello-zeron
   ```

7. **Réinstaller et vérifier le paquet signé** après la signature. Toute modification de source, SDK, manifeste ou ressource ensuite rend cette signature périmée; signe de nouveau. Le paquet publié doit inclure le SDK et toutes ses ressources, mais pas le clone de travail, les journaux, caches ou secrets.

La CLI et l’écran d’installation peuvent changer. Si la version courante diffère du workflow ci-dessus, suis le retour de la CLI et de l’installateur plutôt que d’utiliser une option non documentée.

## Contrôles avant de livrer

Utilise cette liste à chaque modification du plugin :

- [ ] Le squelette et `zeron.luau` viennent de la même version de Zeron que celle visée.
- [ ] Le `id` du manifeste correspond à l’appel `require("./zeron")(id)`.
- [ ] Chaque outil déclaré a une implémentation de même nom; les arguments sont validés à l’exécution et la sortie suit un format stable.
- [ ] Chaque permission correspond à un appel concret. Les commandes autorisées sont listées explicitement et n’acceptent pas de chaîne shell.
- [ ] Les chemins, URLs, IDs, nombres et tailles sont bornés; les données agent, fichiers, GitHub et appareils sont traitées comme non fiables.
- [ ] Les erreurs asynchrones sont montrées à l’utilisateur ou au caller. Les callbacks/listeners peuvent être désinscrits et ne mettent pas à jour un composant détruit.
- [ ] Les opérations non idempotentes ne sont pas renvoyées automatiquement après timeout. Les effets de bord ambigus sont présentés comme incertains, avec une action de vérification.
- [ ] Les imports/export et le stockage n’incluent aucun secret; les dumps/logs sont bornés et expurgent les données sensibles.
- [ ] Le plugin fonctionne lorsque les permissions facultatives sont refusées, lorsque la donnée de contexte manque et lorsque le service distant/appareil est hors ligne.
- [ ] La version signée a été installée et parcourue dans Zeron. La signature a été refaite après le dernier changement.

## Diagnostic des erreurs fréquentes

| Symptôme | Vérifications rapides |
| --- | --- |
| Le plugin ou sa page n’apparaît pas | JSON du manifeste valide, `entry` présent, identifiant passé au SDK identique, permission `pages.add`, plugin activé et rechargé. |
| « not allowed » ou refus de capacité | Repérer l’appel qui échoue; déclarer la permission précise, resigner, réinstaller et demander le consentement de l’utilisateur. Ne pas élargir les permissions sans identifier la capacité requise. |
| Un outil d’agent n’est pas proposé | Permission `tools`, outil du manifeste et `zeron.tool` du même nom, schéma JSON valide, description et champs `required` cohérents. Vérifier aussi le harness/modèle utilisé. |
| Une commande échoue | Binaire autorisé dans `commands`, arguments séparés, chemin courant valide, programme installé sur l’appareil d’exécution et timeout adapté. `zeron.run` n’est pas un shell. |
| Le callback asynchrone ne semble jamais finir | Vérifier que les deux callbacks `:after` sont installés, afficher les refus `zeron.reason(err)`, vérifier device/network/permission, et ne pas bloquer l’UI. |
| Un événement est manqué ou traité deux fois | Attendre la promesse d’inscription retournée par `zeron.on`; filtrer sur IDs de chat/run; gérer l’ordre et dédupliquer les IDs reçus; conserver la fonction de désinscription. |
| Une modification du plugin ne semble pas prise en compte | Vérifier que la copie de développement est celle qui est activée, recharger/réinstaller puis signer après la dernière modification. |
| Une erreur apparaît après timeout | Ne pas supposer que l’effet n’a pas eu lieu. Lire l’état distant ou le journal, marquer le résultat comme inconnu puis laisser l’utilisateur décider d’une reprise. |

## Modèles d’implémentation sûrs

### Appel asynchrone avec résultat borné

L’API varie selon l’action, mais le contrôle doit garder la même forme : valider avant l’appel, traiter les deux branches, contrôler la taille avant l’affichage ou la persistance.

```lua
local function load_summary(id)
    if type(id) ~= "string" or #id == 0 or #id > 128 then
        return nil, "Identifiant invalide"
    end

    local result, failure
    -- Dans une UI, conserve un état loading/error/result et mets-le à jour
    -- dans success/failure; l'exemple omet ce composant d'état.
    some_async_operation(id):after(function(value)
        result = value
    end, function(err)
        failure = zeron.reason(err)
    end)
    return result, failure
end
```

Ce pseudocode montre seulement la séparation validation/succès/échec : il n’attend pas la fin de la promesse. Le code UI réel doit appliquer le résultat dans un état réactif et ne pas retourner prématurément une valeur que le callback n’a pas encore reçue.

### Opération agent à corréler

Orchestrator Builder montre un modèle utile lorsque des événements, appels d’outils et envois de messages peuvent arriver dans des ordres différents : crée un identifiant de run et un nonce d’étape, lie le tool call au chat appelant, persiste le reçu avant l’avancement, tolère les doublons identiques et bloque en cas de rapport contradictoire ou de livraison inconnue. Un `turn.finished` seul ne prouve pas que l’outil attendu a répondu. Le texte final de l’agent ne remplace pas le reçu corrélé.

Pour les workflows qui déclenchent chats ou effets externes, ne copie pas l’orchestration complète de Builder par défaut : le pattern est destiné aux tâches réellement distribuées et durables.

### Service et UI

Une UI et un service ont des cycles de vie différents. Le service convient au travail sans fenêtre, aux événements inter-appareils et aux tâches périodiques; la page convient à la présentation et aux interactions directes. Si une tâche doit survivre à la fermeture de la fenêtre, place-la dans un service et déclare les capacités exigées par le manifeste et le runtime. Dans les deux cas, ajoute un arrêt explicite pour les timers/listeners, borne les messages et gère `offline`, `not_installed`, `version_mismatch` et `timeout`.

`zeron.every` a un minimum de 30 secondes; ce n’est pas une minuterie temps réel. Les messages inter-appareils doivent tenir dans la limite de 64 Ko et leurs champs doivent être validés comme une entrée réseau.

## Documentation et sources

- [DeepWiki — zeronsh/zeron](https://deepwiki.com/zeronsh/zeron) : architecture du produit (UI, moteur, RPC, agents, sessions, synchronisation), pas un index exhaustif des API plugin.
- [DeepWiki — interface desktop](https://deepwiki.com/zeronsh/zeron/2-desktop-ui-%28zeron-ui-gpui%29) : shell et état UI.
- [DeepWiki — journaux et orchestration des sessions](https://deepwiki.com/zeronsh/zeron/3.3-session-orchestration-and-run-journals) : fonctionnement interne des sessions.
- [DeepWiki — Agent Harnesses](https://deepwiki.com/zeronsh/zeron/4-desktop-ui-%28zeron-ui%29) : abstraction interne des runtimes agents.
- [Architecture Zeron sur GitHub](https://github.com/zeronsh/zeron/blob/main/ARCHITECTURE.md).
- [Guide de création du Builder](https://github.com/Master0fFate/zeron-orchestration-builder/blob/initial-release/docs/creer-un-plugin-zeron.md) : procédure complète et référence annotée de son SDK.
- Sources locales : [SDK plugin](../plugin/zeron.luau), [manifeste Graphimo](../plugin/zeron-plugin.json), [bridge marimo](../plugin/marimo_bridge.luau), [Builder README](https://github.com/Master0fFate/zeron-orchestration-builder/blob/initial-release/README.md), et ses modules `graph.luau`, `runner.luau`, `changes.luau`.

La DeepWiki est générée et peut être en retard sur une release. Pour une signature plugin précise, compare le SDK créé par `zeron plugin new`, le manifeste et les permissions de la version de Zeron effectivement installée.
