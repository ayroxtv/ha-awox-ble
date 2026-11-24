# Protocole Telink mesh des lumières AwoX / EGLO connect

Ce document décrit le protocole tel qu'il est implémenté dans :

- `python-awox-mesh-light` (Leiaz, 2017, MIT) : crypto et commandes ;
- `home-assistant-awox` (Frans Saris, 2020, MIT) : parsing des statuts, API cloud ;
- `EspHome-AwoX-BLE-mesh-hub` (Frans Saris, MIT) : vérification croisée de la couche statut (0xDC) et des rapports 0xD8 / 0xEB.

Rien n'a été réinventé : `custom_components/awox_ble/protocol/` est un portage, et `tests/protocol/test_parity.py` vérifie octet par octet que les paquets produits sont identiques à ceux de l'ancien code.

Notation : `rev(x)` = octets de `x` dans l'ordre inverse. Les entiers multi-octets sont en little endian.

## 1. GATT

Service `00010203-0405-0607-0809-0a0b0c0d1910`.

| Caractéristique | UUID (suffixe) | Usage |
|---|---|---|
| Status / notifications | `…1911` | notifications chiffrées ; écrire `01` active les notifications |
| Command | `…1912` | écriture des commandes chiffrées (20 octets) |
| OTA | `…1913` | mise à jour firmware (non utilisée) |
| Pair | `…1914` | login et changement de mesh |

Caractéristiques standard lues par l'ancienne intégration : model `0x2A24`, firmware `0x2A26`, hardware `0x2A27`.

## 2. Chiffrement de base

`E(key, data)` = AES-128-ECB appliqué à `rev(key)` et `rev(pad16(data))`, puis on inverse le résultat :

```
E(key, data) = rev( AES_ECB(rev(key)).encrypt( rev(data.ljust(16, 00)) ) )
```

## 3. Login (pairing de session)

1. Le client tire `session_random` (8 octets).
2. `np = pad16(mesh_name) XOR pad16(mesh_password)` (16 octets maximum chacun).
3. Écriture sur **Pair** : `0C || session_random || E(pad16(session_random), np)[0:8]` (17 octets).
4. Lecture de **Pair** :
   - `0D || response_random(8) …` → succès ;
   - `0E …` → identifiants mesh refusés (appareil appairé à un autre compte) ;
   - autre → erreur.
5. Clé de session : `session_key = E(np, session_random || response_random)`.

Changement de mesh (appareil en usine `unpaired` / `1234`) : écrire successivement `04 || E(sk, name)`, `05 || E(sk, password)`, `06 || E(sk, ltk)` sur **Pair**, puis relire : `07` = accepté. Non utilisé par `awox_ble` (lecture seule de la configuration existante).

## 4. Activation des notifications

Après le login, écrire `01` sur la **valeur** de **Status** (`…1911`) : c'est ce qui déclenche l'envoi des notifications côté Telink.

**Il n'y a pas de CCCD.** Mesuré sur un EGLO EPanel_120 : `…1911` (handle 17, propriétés read/write/notify) n'a qu'un descripteur `0x2901`. Conséquences :

- `gatttool` (ancienne intégration) lit l'ATT brut et reçoit toutes les notifications sans rien écrire d'autre ; c'est pour ça que l'ancien code écrivait sur la poignée de valeur au lieu du CCCD.
- **BlueZ** (adaptateur local de HA) synthétise un CCCD à l'emplacement du `0x2901` et y écrit lors de `StartNotify` / `AcquireNotify`. Le nœud ne répond jamais ; au bout du délai ATT de 30 s, BlueZ coupe la liaison (`UNLIKELY_ERROR`). Les notifications arrivent pendant ces 30 s. Voir [bluez/bluez#2383](https://github.com/bluez/bluez/issues/2383), fermé sans correctif.
- **Proxy ESPHome** : `bluetooth_gatt_start_notify` s'abonne sur l'ESP sans écrire de CCCD ; c'est bleak-esphome qui l'écrit ensuite. On appelle donc l'API du proxy directement. Voir [bleak-esphome#447](https://github.com/Bluetooth-Devices/bleak-esphome/issues/447).
- Lire `…1911` ne renvoie qu'un octet : impossible d'obtenir l'état par lecture.
- Après une déconnexion, le nœud cesse parfois d'émettre ses annonces pendant ~20 s.

## 5. Commandes (client → mesh)

Payload en clair (15 octets) :

```
dest(2, LE) || opcode(1) || vendor 60 01 || data (≤ 10) || padding 00
```

`dest` est l'adresse mesh de la cible (`0xFFFF` = tout le mesh). Le nœud auquel on est connecté relaie.

Nonce (8 octets) : `rev(MAC_passerelle)[0:4] || 01 || seq(3)`. **La MAC utilisée est celle du nœud auquel on est connecté**, pas celle de la destination.

- `check = CBCMAC(session_key, nonce, payload)` :
  `c = E(sk, pad16(nonce || len(payload)))`, puis pour chaque bloc de 16 : `c = E(sk, c XOR bloc)`.
- `enc = CTR(session_key, nonce, payload)` : flux `E(sk, pad16(00 || nonce))`, le premier octet du compteur est incrémenté à chaque bloc.
- Paquet écrit sur **Command** (20 octets) : `seq(3) || check[0:2] || enc(15)`.

Numéro de séquence : l'ancien code tire 3 octets aléatoires par commande. `awox_ble` utilise un compteur 24 bits à départ aléatoire (jamais 0), ce qui évite qu'un nœud prenne une commande pour un doublon déjà relayé.

### Opcodes et données

| Opcode | Nom | Données |
|---|---|---|
| `D0` | power | `01` on / `00` off |
| `E2` | couleur | `04 R G B` |
| `F2` | luminosité couleur | 1 octet, `0x0A`–`0x64` |
| `F1` | luminosité blanc | 1 octet, `0x01`–`0x7F` |
| `F0` | température blanc | 1 octet, `0x00` (froid, 6500 K) – `0x7F` (chaud, 2700 K) |
| `DA` | demande de statut | `10` |
| `C8` | preset (séquence) | 1 octet, 0–6 |
| `C9` | mode bougie | `00` |
| `F5` / `F6` | durée couleur / fondu d'une séquence | uint32 LE, ms |
| `E0` | changer l'adresse mesh | uint16 LE |
| `E3` | reset mesh | `00` |
| `EA` | requête infos appareil | `10 00` (MAC/produit), `10 02` (version), `10 13` (mesure de puissance, prises) |
| `DD` | requête groupes | `0A 01` |

## 6. Rapports (mesh → client, notifications sur Status)

Déchiffrement : nonce = `rev(MAC_passerelle)[0:3] || paquet[0:5]`. `paquet[0:3]` = séquence, `paquet[3:5]` = adresse source, `paquet[5:7]` = checksum. `clair = paquet[0:7] || CTR(sk, nonce, paquet[7:20])`. Le paquet est rejeté si `CBCMAC(sk, nonce, clair[7:])[0:2] != paquet[5:7]`.

Octet 7 du clair = opcode.

### `DB` : réponse à `DA`

| Octet | Contenu |
|---|---|
| 3–4 | adresse mesh (LE) |
| 10 | mode : bit0 on, bit1 couleur, bit2 séquence, bit4 bougie |
| 11 | luminosité blanc |
| 12 | température blanc |
| 13 | luminosité couleur |
| 14–16 | R, G, B |

### `DC` : statut spontané (changement d'état, présence en ligne)

| Octet | Contenu |
|---|---|
| 10 | adresse mesh, octet bas |
| 11 | 0 = hors ligne |
| 12 | mode (mêmes bits) |
| 13–18 | luminosité blanc, température, luminosité couleur, R, G, B |
| 19 | adresse mesh, octet haut |

L'ancienne intégration ignorait les `DC` et faisait du polling. `awox_ble` les exploite : c'est ce qui rend l'état à jour quand on change la lumière depuis l'appli AwoX.

### Autres rapports (journalisés, pas encore exploités)

- `D8` avec `[10] == 00` : MAC (octets 13–16, inversés) et produit ; avec `[10] == 13` : tension/puissance (prises).
- `EB` avec `[10] == 10` : identifiant produit et suffixe MAC.
- `D4` : liste des groupes (octets 10–19, terminée par `FF`).

## 7. Cloud AwoX (configuration initiale uniquement)

Serveur Parse : `https://l4hparse-prod.awox.cloud/parse/` (repli : `https://l4hparse-hc-prod.awox.cloud/parse/`). En-têtes : `x-parse-application-id`, `x-parse-client-key` (constantes publiques de l'appli), `x-parse-installation-id` (UUID aléatoire), puis `x-parse-session-token` après login.

1. `POST login` `{"username", "password", "_method": "GET"}` → `objectId`, `sessionToken`.
2. `POST classes/Credential` avec `where.owner = Pointer(_User, objectId)` → entrée `service == "mesh"` : `client_id` = **mesh name**, `access_token` = **mesh password**, `refresh_token` = **long term key**.
3. `POST classes/Device` (même filtre) → pour chaque appareil dont `type` commence par `.ble.tlmesh` : `address` (adresse mesh), `macAddress`, `displayName`, `modelName`, `vendor`, `version`, `hardwareVersion`, `type` (contient `light`, `color`, `temperature`, `dimming`, `plug`…).

Après la configuration, `awox_ble` n'appelle plus jamais le cloud.

## 8. Annonces Bluetooth (à confirmer sur le matériel)

L'ancien code repère les appareils par le préfixe MAC Telink `A4:C1` (partagé avec d'autres produits Telink, par exemple des capteurs Xiaomi, donc insuffisant seul). Dans le SDK Telink, les données constructeur contiennent les 4 octets bas de la MAC inversés, ce qui suffit au nonce (seuls `rev(MAC)[0:4]` sont utilisés). La CLI (`tools/awox_cli.py scan`) affiche les annonces brutes pour confirmer le format et ajuster le matcher de `manifest.json`.
