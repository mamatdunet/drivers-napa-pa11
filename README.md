# PA11 sur un Mac d'aujourd'hui

Outil pour lire et remplir le lecteur MP3 **Napa / A-Max PA11** (2003) depuis un Mac récent (Apple Silicon ou Intel), sans pilote système.

## Pourquoi le Mac ne voit pas le lecteur

Le PA11 est construit autour d'une puce SigmaTel STMP3400 (identifiant USB `066F:004B`). Contrairement aux lecteurs plus récents, il ne se présente pas comme une clé USB. Son firmware USB donne seulement un accès brut à la mémoire flash (format SmartMedia). C'était le pilote de l'époque, sur l'ordinateur, qui reconstruisait la table des blocs et vérifiait les codes correcteurs (ECC).

`pa11.py` refait exactement ce travail en espace utilisateur, via libusb. Le protocole a été retrouvé en désassemblant le pilote Mac OS 9 d'origine (`066F004BUsb`, 2002), qui contenait encore ses noms de fonctions. Les codes correcteurs et l'encodage des adresses de blocs ont été comparés bit à bit avec ce pilote, exécuté dans un émulateur PowerPC.

## Installation (une fois)

1. Téléchargez ce dépôt (bouton **Code → Download ZIP**) et décompressez-le.
2. Faites un clic droit sur **install.command**, puis **Ouvrir** (macOS demande une confirmation la première fois). Vous pouvez aussi passer par le Terminal : `bash install.command`.
3. Le script installe l'outil dans le dossier **~/PA11**, avec son propre environnement Python (pyusb + libusb). Votre système n'est pas modifié.

Si Python manque, macOS propose d'installer les « outils de ligne de commande » d'Apple. Acceptez, puis relancez `install.command`.

## Utilisation

Branchez le PA11 avec un adaptateur USB-A → USB-C. S'il est fonctionnel, il passe en mode USB. Si macOS demande d'**autoriser l'accessoire**, acceptez.

Dans le dossier **~/PA11** :

| Raccourci | Effet |
|---|---|
| **1 - Diagnostic PA11** | Vérifie que le lecteur répond et affiche la mémoire, les blocs et le volume. **Lecture seule.** |
| **2 - Ouvrir PA11** | La première fois, fait une sauvegarde brute complète (environ 1 à 2 min). Copie ensuite le contenu du lecteur dans une image disque et l'ouvre dans le Finder. |
| **3 - Fermer PA11** | Démonte l'image, supprime les fichiers cachés ajoutés par macOS et réécrit sur le lecteur **uniquement les blocs modifiés**, avec relecture de vérification. |

Entre « Ouvrir » et « Fermer », copiez ou supprimez vos MP3 dans le Finder, comme sur une clé USB. Laissez le lecteur branché et ne l'utilisez pas pendant ce temps.

Les mêmes actions existent en ligne de commande : `~/PA11/pa11 info | backup | open | close | pull | push`. Ajoutez `--debug` pour afficher les échanges USB.

## Sécurité

- **Le firmware du lecteur n'est jamais touché.** Les blocs qui le contiennent, le bloc CIS et les blocs défectueux sont protégés par un garde-fou dans le code.
- Aucune écriture n'a lieu tant qu'une sauvegarde brute n'existe pas (`~/PA11/sauvegarde-brute-*.bin`).
- Avant d'écrire un bloc, l'outil vérifie qu'il n'a pas changé sur le lecteur depuis la copie. Après l'écriture, il relit le bloc pour contrôler le résultat.
- La FAT et le répertoire sont écrits en dernier. Si l'écriture est interrompue, l'ancien index reste cohérent.
- Si un bloc de flash usé refuse de s'effacer, ses données sont déplacées vers un bloc libre et l'ancien bloc est marqué défectueux (norme SmartMedia).
- Le lecteur doit avoir une pile correcte : l'outil refuse d'écrire si le lecteur signale une alimentation faible. Cette alerte peut se déclencher à tort, même avec des piles neuves. Dans ce cas, relancez l'écriture dans le Terminal avec `~/PA11/pa11 close --ignorer-alimentation`.

## Limites connues

- L'outil n'a été testé que sur un **lecteur simulé** qui reproduit le protocole : les tests de lecture, écriture, correction d'erreurs, blocs défectueux et tailles de 8, 64 et 128 Mo passent tous. **Il n'a pas encore été testé sur un vrai PA11.** Commencez par « Diagnostic » (lecture seule).
- En cas d'erreur, relancez avec `~/PA11/pa11 --debug info` et conservez la sortie : elle suffit pour corriger l'outil.
- Le mode « récupération » (lecteur sans firmware, USB `066F:3400/3410`) et le reformatage complet ne sont pas pris en charge.

## Fichiers

- `pa11.py`, l'outil (un seul fichier, Python 3.7 ou plus récent)
- `install.command`, l'installation pour macOS
- `README.md`, ce guide
- `pa11sim.py` et `test_sim.py`, le lecteur simulé et les tests (Linux, avec mtools et dosfstools)
