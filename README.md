# AwoX / EGLO connect (BLE mesh) pour Home Assistant

Intégration `awox_ble` : pilote en local les lumières AwoX / EGLO connect (protocole Telink mesh) via la pile Bluetooth de Home Assistant, adaptateur local **et** proxys Bluetooth ESPHome.

C'est une réécriture de [`fsaris/home-assistant-awox`](https://github.com/fsaris/home-assistant-awox) : le protocole est porté tel quel (vérifié octet par octet par les tests), mais tout le transport est remplacé.

| | `awox` (ancienne) | `awox_ble` |
|---|---|---|
| Transport | `pygatt` + `gatttool` en sous-processus | `bleak` + `bleak-retry-connector` via la pile Bluetooth de HA |
| Découverte | scan `bluetoothctl` bloquant de 20 s | annonces déjà reçues par HA (aucun scan manuel) |
| État | polling toutes les ~30 s | notifications poussées (`local_push`) |
| Proxys ESPHome | non | oui, choix du meilleur nœud et du meilleur scanner |
| Dépendances Python | `pygatt`, `pexpect`, `pycryptodome` | aucune (tout est déjà dans HA) |

## Fonctionnement

- Une seule connexion GATT au mesh : n'importe quel nœud relaie vers les autres. On choisit le nœud connectable qui a le meilleur RSSI parmi tous les scanners, et on bascule sur un autre si la connexion tombe ou si un proxy n'a plus de slot libre.
- Login Telink, abonnement aux notifications, puis **une seule** demande de statut juste après la connexion. Ensuite, tout arrive par notifications, y compris quand la lumière est changée depuis l'appli AwoX.
- Les commandes passent par une file qui ne bloque jamais l'interface. L'état est mis à jour de façon optimiste, puis confirmé par la notification. Les commandes répétées (curseur de luminosité) sont fusionnées ; une commande qui n'a pas pu partir en 10 s est abandonnée.
- Reconnexion automatique : premier essai après 0,5 s, puis backoff exponentiel plafonné à 60 s. Les entités ne passent « indisponible » que si le mesh reste injoignable 30 s.
- Keepalive : une lecture GATT légère après 60 s sans trafic détecte les connexions mortes.
- Aucun appel bloquant, aucun thread, aucun sous-processus.

### Adaptateur local ou proxy ESPHome

Ces lumières n'ont pas de descripteur CCCD, et la pile Bluetooth locale de HA (BlueZ) coupe la liaison au bout de 30 s quand on active les notifications ([bluez/bluez#2383](https://github.com/bluez/bluez/issues/2383), voir `docs/PROTOCOL.md`).

| Chemin | Comportement |
|---|---|
| Proxy Bluetooth ESPHome | Liaison stable **et** état poussé en temps réel (abonnement direct sur le proxy). Recommandé : un ESP32 à quelques euros suffit. |
| Adaptateur local, mode **stable** (défaut) | Liaison stable, commandes immédiates. Pas de remontée d'état : HA garde l'état qu'il a envoyé (et le dernier état connu après redémarrage). Un changement fait depuis l'appli AwoX ou une télécommande n'est pas vu. |
| Adaptateur local, mode **cycles** | L'état remonte, mais la liaison est relancée toutes les ~27 s ; le panneau mettant parfois ~20 s à se réannoncer, une commande peut attendre jusqu'à ~25 s. |

Le mode se choisit dans *Paramètres → Appareils et services → AwoX / EGLO connect → Configurer*.

## Installation

HACS (dépôt personnalisé, catégorie *Integration*) ou copie manuelle de `custom_components/awox_ble` dans `/config/custom_components/`.

Prérequis : Home Assistant 2026.10 ou plus récent, intégration **Bluetooth** active (adaptateur local et/ou proxy ESPHome avec `bluetooth_proxy: active: true`).

## Configuration

*Paramètres → Appareils et services → Ajouter une intégration → AwoX / EGLO connect*, ou la carte de découverte Bluetooth.

- **Compte AwoX** (recommandé) : utilisé une seule fois pour lire le nom et le mot de passe du mesh et la liste des appareils. Le mot de passe du compte n'est pas enregistré. Ensuite, plus aucun appel au cloud.
- **Saisie manuelle** : nom et mot de passe du mesh, plus la MAC d'une lumière. HA s'y connecte et crée une lumière par nœud qui répond.

## Migration depuis `awox`

Les deux intégrations peuvent être installées en même temps (domaines différents), mais **deux clients ne peuvent pas tenir le mesh en même temps**. Désactivez l'entrée `awox` (*⋮ → Désactiver*) avant d'activer `awox_ble`.

## Journaux de debug

```yaml
logger:
  default: warning
  logs:
    custom_components.awox_ble: debug
    bleak_retry_connector: debug
    homeassistant.components.bluetooth: debug
```

Les identifiants du mesh et la clé de session ne sont jamais journalisés. Les diagnostics (*⋮ → Télécharger les diagnostics*) les masquent et exposent les compteurs utiles : connexions, reconnexions, délai de la dernière reconnexion, slots saturés, commandes envoyées/fusionnées/abandonnées, notifications.

## Développement

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/mypy custom_components tests && .venv/bin/pytest
```

- `custom_components/awox_ble/protocol/` : protocole pur Python (aucun import HA, aucune I/O).
- `tests/protocol/test_parity.py` : parité octet par octet avec l'ancien code (copie de `packetutils.py` dans `tests/protocol/legacy/`).
- `tests/fake_device.py` : faux nœud Telink qui chiffre et déchiffre réellement, pour tester le `MeshManager`, le config flow et l'entité.
- `docs/PROTOCOL.md` : le protocole documenté.

### CLI de test sur le vrai matériel

`tools/awox_cli.py` n'utilise que bleak, sans HA :

```bash
.venv/bin/python tools/awox_cli.py scan --mac A4:C1:38:12:34:56
.venv/bin/python tools/awox_cli.py cloud --username vous@example.com --save mesh.json
.venv/bin/python tools/awox_cli.py run --config mesh.json --mac A4:C1:38:12:34:56 on brightness 128 color 255 0 0 temp 3000 off listen 10
.venv/bin/python tools/awox_cli.py bench --config mesh.json --mac A4:C1:38:12:34:56 --count 20
```

Sur macOS, CoreBluetooth cache les adresses MAC alors que le nonce Telink en a besoin : passez la vraie MAC avec `--mac`. La CLI retrouve l'appareil dans les annonces (les données constructeur contiennent les 4 octets bas de la MAC), ou via `--name` / `--address`. `mesh.json` contient les identifiants du mesh : il est en `chmod 600` et ignoré par git.

### Déployer sur Home Assistant

```bash
HA_SSH=root@homeassistant.local scripts/deploy.sh
```

Le script copie l'intégration via l'add-on SSH (tar sur ssh), puis lance `ha core restart`.

## Crédits

- [Leiaz / python-awox-mesh-light](https://github.com/Leiaz/python-awox-mesh-light) (MIT) : rétro-ingénierie du protocole, crypto et commandes.
- [Frans Saris / home-assistant-awox](https://github.com/fsaris/home-assistant-awox) et [EspHome-AwoX-BLE-mesh-hub](https://github.com/fsaris/EspHome-AwoX-BLE-mesh-hub) (MIT) : statuts, API cloud, rapports mesh.

Licence MIT ; les notices de copyright d'origine sont conservées dans `LICENSE`.
