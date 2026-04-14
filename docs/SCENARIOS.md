# Battle Scenarios — design notes

Ten classic battles adapted to Clash of Odin. Each is sized to the
engine's current mechanics (two teams, HP/ATK/DEF/RES/MOVE/RANGE,
tag-less v1 combat, custom glyph+color per class, optional `rules.py`
plugins and narrative events). No MP, no abilities — everything is
positional + stat.

Numbering continues from the existing shipped scenarios:

  01_tiny_skirmish, 02_basic_mirror, journey_to_the_west,
  03_thermopylae, 04_cannae, 05_stirling_bridge, 06_agincourt,
  07_red_cliffs, 08_kadesh, 09_troy, 10_little_bighorn,
  11_helms_deep, 12_five_armies

Legend per class table: **HP / ATK / DEF / RES / SPD / MOVE / RANGE**.

---

## 03_thermopylae — "The Hot Gates" (480 BC)

**Story.** Xerxes I marches west with the largest army the ancient
world has ever seen. Three hundred Spartans under Leonidas, plus
several thousand allied Greeks, bottle him up at a 50-foot coastal
pass between cliffs and the sea. For three days a handful of heavily-
armored hoplites holds the narrowest choke point on the road to
Athens. This is the choke-point classic.

**Map.** 14 × 6. Cliffs (impassable) along the top and bottom rows
leave a three-tile-wide corridor. A single "gate" column of fort
tiles marks Leonidas's defensive line at x=4. The east edge (x=13)
is Xerxes's spawn; the west edge (x=0) is the road to Athens.

```
Legend: ^ cliff (impassable)  = fort/gate  . plain  o hoplite shield-wall anchor
```

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue | leonidas      | 40 | 11 | 8 | 4 | 6 | 4 | 1–1 |
| blue | spartan       | 28 | 9  | 7 | 3 | 5 | 3 | 1–1 |
| blue | greek_hoplite | 22 | 7  | 6 | 3 | 4 | 3 | 1–1 |
| red  | immortal      | 26 | 10 | 5 | 3 | 6 | 4 | 1–1 |
| red  | persian_infantry | 14 | 5 | 3 | 2 | 4 | 3 | 1–1 |
| red  | persian_archer   | 12 | 7 | 2 | 2 | 5 | 4 | 2–3 |

**Forces.** Blue: 1 Leonidas + 3 Spartans + 6 Greek hoplites (= 10).
Red: 2 Immortals + 10 Persian infantry + 6 Persian archers (= 18).

**Win conditions.**
- Blue loses if Leonidas dies (`protect_unit`).
- Blue wins if Leonidas survives turn 25 (`max_turns_draw` aliased
  as survival for the defender).
- Red wins by eliminating blue OR crossing a unit to x<=1
  (`reach_goal_line` on axis=x, value=1, team=red).

**ASCII art beat.** Shield-wall frames for Spartans (hoplite holding
aspis vs thrusting spear); Immortals with square shields.

**Mechanic showcase.** Chokepoint + `reach_goal_line` + asymmetric
defender-wins-by-surviving. Mountain tiles demonstrate
`can_enter_mountain` per-class (Spartans can't, hill tribes might).

---

## 04_cannae — "Hannibal's envelopment" (216 BC)

**Story.** Hannibal Barca crosses the Alps with Carthaginian and
Gallic mercenaries and meets Rome on an open plain in southern
Italy. The Romans field 80,000 men in a deep infantry block and
expect to crush everything by weight. Hannibal arranges his line
concave: the center retreats as the Romans press, and his Libyan
spearmen fold in on both flanks. When the Numidian cavalry closes
the back, 50,000 Romans die in a single afternoon. The textbook
example of *the double envelopment*.

**Map.** 16 × 10. Flat open plain. A river runs along the north
edge (y=0) blocking that flank. A slight rise (forest-equivalent at
(7,5), (8,5), (9,5)) marks the Roman tribune's hill.

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (Rome) | roman_legionary | 22 | 8 | 6 | 3 | 5 | 3 | 1–1 |
| blue | roman_hastati     | 18 | 7 | 5 | 3 | 5 | 3 | 1–1 |
| blue | varro             | 30 | 7 | 5 | 3 | 5 | 3 | 1–1 |
| red (Carthage) | libyan_spearman  | 20 | 7 | 5 | 3 | 5 | 3 | 1–2 |
| red  | gallic_warrior    | 18 | 9 | 3 | 2 | 6 | 4 | 1–1 |
| red  | numidian_cavalry  | 22 | 8 | 4 | 3 | 8 | 6 | 1–1 |
| red  | hannibal          | 30 | 10 | 6 | 4 | 7 | 5 | 1–1 |

**Forces.** Blue: Varro + 12 legionaries + 6 hastati (= 19). Red:
Hannibal + 6 Libyans + 6 Gauls + 4 Numidian cavalry (= 17). Red is
outnumbered but faster with cavalry on both flanks.

**Win conditions.**
- Red wins if Varro dies (`protect_unit`).
- Either side wins by eliminating the other.
- Max turns 30 → draw.

**Mechanic showcase.** Cavalry speed + flanking. No special plugins
— pure stats + positioning.

---

## 05_stirling_bridge — "Wallace at the Forth" (1297)

**Story.** The English cross the River Forth on a narrow wooden
bridge two horses wide. William Wallace and Andrew Moray let half
the English column cross, then charge down from Abbey Craig and
pin them against their own river. The bridge becomes a bottleneck
that kills more English than Scottish blades do.

**Map.** 12 × 10. A river splits the map top/bottom; a single
bridge tile at (6, 5) is the only passage. Abbey Craig (high ground,
+DEF) at (2, 2)–(4, 3). English camp at the south edge.

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (Scots) | wallace        | 38 | 13 | 7 | 4 | 7 | 5 | 1–1 |
| blue | andrew_moray    | 28 | 9  | 6 | 3 | 6 | 4 | 1–1 |
| blue | scots_spearman  | 18 | 7  | 5 | 3 | 5 | 3 | 1–2 |
| blue | highland_swordsman | 22 | 9 | 4 | 2 | 6 | 4 | 1–1 |
| red (England) | english_knight    | 30 | 10 | 7 | 3 | 4 | 3 | 1–1 |
| red  | english_longbowman | 16 | 8 | 2 | 2 | 5 | 4 | 2–3 |
| red  | english_man_at_arms | 22 | 8 | 5 | 3 | 5 | 3 | 1–1 |

**Forces.** Blue: Wallace + Moray + 4 spearmen + 4 swordsmen (= 10).
Red: 6 knights + 4 longbowmen + 6 men-at-arms (= 16).

**Win conditions.**
- Blue loses if Wallace dies.
- Blue wins by eliminating red OR holding the north bank for 8
  end_turns (a `hold_tile` rule on (6,4) adjacent to the bridge).
- Red wins by crossing 6 units to the north bank.

**Mechanic showcase.** One-tile bridge demonstrates
`passable: false` river tiles + `hold_tile` rule.

---

## 06_agincourt — "Henry's longbows in the mud" (1415)

**Story.** Henry V's exhausted, dysentery-ridden English army is
cornered by a French force three times its size in a narrow muddy
field between two forests. The French knights charge in full plate
through a churned bog; the English longbowmen perch on the flanks
behind sharpened stakes. Ten thousand French are shot down, most
drowned in the mud under the weight of their own armor.

**Map.** 10 × 14. A narrow muddy corridor between two dense forests
at x=0..1 and x=8..9. Stakes (impassable) in the English line at
y=11. English camp at the north, French camp at the south.

```
Terrain: mud (move_cost: 3, no defense bonus) everywhere in the
corridor. Forest (impassable to knights, passable to archers) on
the flanks.
```

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (England) | henry_v        | 32 | 10 | 6 | 4 | 6 | 4 | 1–1 |
| blue | longbowman     | 14 | 10 | 2 | 2 | 5 | 4 | 2–4 |
| blue | english_man_at_arms | 24 | 8 | 6 | 3 | 5 | 3 | 1–1 |
| red (France) | french_knight   | 30 | 11 | 8 | 3 | 4 | 3 | 1–1 | (can't enter forest; move_cost on mud = 3)
| red  | french_crossbow | 14 | 8  | 2 | 2 | 4 | 3 | 2–3 |
| red  | french_man_at_arms | 22 | 8 | 5 | 3 | 5 | 3 | 1–1 |

**Forces.** Blue: Henry + 10 longbowmen + 4 men-at-arms (= 15).
Red: 10 knights + 4 crossbows + 10 men-at-arms (= 24).

**Win conditions.**
- Blue loses if Henry dies (`protect_unit`).
- Blue wins by eliminating red OR surviving turn 20.
- Red wins by reaching the English camp line y=1 with any knight.

**Mechanic showcase.** Per-class terrain override — French knights
can't enter forest (`class_overrides`). Mud is a custom terrain type
that all classes enter at 3 cost. Ranged archers dominate mud-bogged
knights.

---

## 07_red_cliffs — "Chibi fire ships" (208 AD)

**Story.** Late Han dynasty collapsing. Cao Cao unites the north
and marches a massive fleet down the Yangtze to finish off the
south. Sun Quan and Liu Bei ally; the strategist Zhuge Liang
"borrows" the east wind; Huang Gai feigns defection and rams fire
ships into Cao Cao's tethered navy. Overnight, the north burns.

**Map.** 18 × 10. The Yangtze fills the middle rows (y=3..6) as
river tiles; two shorelines flank it. Cao Cao's chained fleet sits
as a line of "ship" tiles along y=4 between x=10..14 (impassable
except to Cao Cao's own). Liu Bei and Sun Quan ally on the south
bank; Cao Cao on the north bank + his ships.

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (Alliance) | zhuge_liang   | 22 | 6 | 4 | 8 | 6 | 4 | 1–2 | (magic)
| blue | zhou_yu       | 30 | 10 | 6 | 4 | 7 | 5 | 1–1 |
| blue | guan_yu       | 42 | 13 | 7 | 4 | 6 | 4 | 1–1 |
| blue | huang_gai     | 26 | 9  | 5 | 3 | 6 | 4 | 1–1 |
| blue | wu_archer     | 16 | 9  | 2 | 3 | 5 | 4 | 2–3 |
| red (Cao Cao) | cao_cao       | 36 | 11 | 7 | 5 | 5 | 4 | 1–1 |
| red  | cao_guard     | 26 | 9  | 6 | 3 | 5 | 3 | 1–1 |
| red  | northern_infantry | 18 | 7 | 4 | 2 | 4 | 3 | 1–1 |

**Forces.** Blue: Zhuge Liang + Zhou Yu + Guan Yu + Huang Gai + 6
Wu archers (= 10). Red: Cao Cao + 3 guards + 10 infantry on the
ships (= 14).

**Win conditions.**
- Red loses if Cao Cao dies (`protect_unit`).
- Blue wins by eliminating all reds OR by Huang Gai reaching any
  tile adjacent to the ship line (triggers fire plugin — see below).

**Plugin.** `rules.py` exposes `on_turn_start: light_fire` that
fires starting turn 5: if Huang Gai (or any blue hero) is adjacent
to a ship tile, the fire spreads +1 along the chain per turn,
damaging Cao Cao's units on affected tiles for 6 HP/turn. The east
wind is narrated via `on_turn_start: "The wind turns east. A flame
leaps from Huang Gai's boat."`

**Mechanic showcase.** Full plugin + narrative combo. Best demo
of the scenario-authoring system.

---

## 08_kadesh — "Ramses's chariot stand" (1274 BC)

**Story.** Ramses II vs Muwatalli II of Hatti. The earliest battle
in recorded history for which we have a real account (from both
sides — they both claimed victory). Egyptian chariots split into
four divisions; Hittite chariots ambush one of them; Ramses
personally rallies the broken line and counter-charges. Chariots
are the showpiece.

**Map.** 14 × 10. The Orontes river at y=0 (impassable). Kadesh
fortress as a fort tile cluster at (12, 8)–(13, 9). Open plain
otherwise; a ford at (3, 1) lets chariots cross.

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (Egypt) | ramses          | 36 | 11 | 6 | 4 | 7 | 5 | 1–1 |
| blue | egyptian_chariot | 24 | 8 | 4 | 3 | 8 | 7 | 1–1 | (can't enter forest)
| blue | egyptian_infantry | 18 | 7 | 5 | 3 | 5 | 3 | 1–1 |
| blue | egyptian_archer  | 14 | 8 | 2 | 3 | 5 | 4 | 2–3 |
| red (Hatti) | muwatalli       | 32 | 10 | 6 | 4 | 6 | 4 | 1–1 |
| red  | hittite_chariot | 28 | 9 | 5 | 3 | 7 | 6 | 1–1 | (heavy — three-man crew)
| red  | hittite_spearman | 20 | 8 | 5 | 3 | 5 | 3 | 1–2 |

**Forces.** Blue: Ramses + 6 Egyptian chariots + 6 infantry + 4
archers (= 17). Red: Muwatalli + 8 Hittite chariots + 8 spearmen
(= 17).

**Win conditions.**
- Either side wins by eliminating the other commander OR by
  seizing the enemy fort (for blue, that's Kadesh).
- Max turns 30.

**Mechanic showcase.** Chariots demonstrate the `move: 7` extreme +
`can_enter_forest: false` constraint. Cavalry/chariot duel on an
open field is satisfying to watch.

---

## 09_troy — "Achilles and Hector" (Iliad)

**Story.** The Greeks have besieged Troy for nine years. Hector,
Troy's prince and best fighter, has driven the Greeks back to their
ships. Achilles, withdrawn from the war in anger, returns after
Patroclus's death and meets Hector outside the Scaean Gate. The
two heroes dwarf everyone around them; the rest of the scenario is
their supporting cast trying to keep them apart (or bring them
together, depending on team).

**Map.** 16 × 12. Troy's walls (fort-line) form the east edge
(x=14..15) enclosing the gate at (14, 6). Greek ships line the
west edge. The river Xanthus (impassable) runs north-south at x=7
with a ford at (7, 6). Plain between.

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (Greeks) | achilles     | 55 | 17 | 8 | 5 | 9 | 6 | 1–1 |
| blue | odysseus     | 26 | 9  | 5 | 4 | 7 | 5 | 1–1 |
| blue | diomedes     | 28 | 10 | 6 | 3 | 7 | 5 | 1–1 |
| blue | ajax         | 32 | 10 | 7 | 3 | 5 | 4 | 1–1 |
| blue | myrmidon     | 20 | 8  | 5 | 3 | 6 | 4 | 1–1 |
| red (Trojans) | hector       | 50 | 15 | 8 | 5 | 8 | 5 | 1–1 |
| red  | paris        | 18 | 9  | 3 | 3 | 6 | 5 | 2–3 |
| red  | aeneas       | 30 | 10 | 6 | 3 | 6 | 4 | 1–1 |
| red  | trojan_guard | 22 | 8  | 5 | 3 | 5 | 3 | 1–2 |

**Forces.** Blue: Achilles + Odysseus + Diomedes + Ajax + 6
Myrmidons (= 10). Red: Hector + Paris + Aeneas + 6 Trojan guards
(= 9). Nearly even — most of the game hinges on the heroes.

**Win conditions.**
- Greeks lose if Achilles dies (`protect_unit`).
- Trojans lose if Hector dies (`protect_unit`).
- Greeks win additionally if they seize the Scaean Gate (fort at
  (14, 6)).
- Trojans win additionally if they burn a Greek ship (`reach_tile`
  at x=0 any row).

**Mechanic showcase.** Two simultaneous `protect_unit` rules — the
first time we've had mutual VIPs. Plus an asymmetric objective stack
(seize-fort vs reach-tile).

---

## 10_little_bighorn — "Custer's Last Stand" (1876)

**Story.** The US 7th Cavalry under Lt. Colonel George Armstrong
Custer attacks a massive Lakota + Cheyenne encampment on the Little
Bighorn River. Custer splits his command and advances on a force
outnumbering him 3–1 led by Sitting Bull and Crazy Horse. His
battalion is surrounded on a ridge and annihilated to the last man.

**Map.** 12 × 14. The Little Bighorn river at x=3 (impassable, with
two fords). Rolling hills (forest-equivalent, +DEF) near the
center. Custer's ridge as a small fort cluster at (8, 4)–(9, 4).
Native encampment south (y > 10).

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (7th Cav) | custer          | 30 | 11 | 5 | 3 | 7 | 5 | 1–1 |
| blue | trooper         | 18 | 8 | 3 | 2 | 6 | 5 | 1–1 |
| blue | cav_sharpshooter | 14 | 10 | 2 | 2 | 5 | 4 | 2–3 |
| red (Lakota/Cheyenne) | sitting_bull   | 28 | 8 | 5 | 4 | 6 | 4 | 1–1 |
| red  | crazy_horse    | 32 | 12 | 5 | 3 | 9 | 6 | 1–1 |
| red  | lakota_warrior | 20 | 9  | 4 | 3 | 7 | 5 | 1–2 |
| red  | cheyenne_warrior | 18 | 9 | 3 | 3 | 7 | 5 | 1–1 |
| red  | native_archer  | 14 | 9  | 2 | 2 | 6 | 4 | 2–3 |

**Forces.** Blue: Custer + 10 troopers + 4 sharpshooters (= 15).
Red: Sitting Bull + Crazy Horse + 12 Lakota + 10 Cheyenne + 6
archers (= 30). Heavily outnumbered.

**Win conditions.**
- Blue loses if Custer dies (`protect_unit` — historical outcome).
- Blue wins by surviving turn 25 (`max_turns_draw` aliased).
- Red wins normally by elimination.

**Mechanic showcase.** Large asymmetric numbers + fast mounted red
forces. `protect_unit` against impossible odds — Claude has to get
*creative* to survive 25 turns.

---

## 11_helms_deep — "Hornburg siege" (Tolkien, LoTR)

**Story.** Saruman's Uruk-hai storm the Hornburg at night while
Théoden's Rohirrim and a contingent of Galadhrim elves defend. The
wall holds until a berserker plants explosives in the culvert.
Gandalf arrives at dawn with Éomer's cavalry and breaks the siege.

**Map.** 16 × 10. The Hornburg's outer wall (fort-line) at x=4..5
from y=1..8, with the Deeping Wall gate at (4, 5). Inside the walls
on the west (x=0..3), defenders. East of the walls: Uruk-hai
attackers. The Deep cliff to the south (impassable).

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (Rohan/Elves) | theoden       | 34 | 10 | 7 | 4 | 6 | 4 | 1–1 |
| blue | aragorn       | 38 | 12 | 6 | 4 | 7 | 5 | 1–1 |
| blue | legolas       | 22 | 10 | 3 | 3 | 8 | 5 | 2–4 |
| blue | gimli         | 28 | 11 | 6 | 3 | 5 | 3 | 1–1 |
| blue | galadhrim_archer | 16 | 9 | 2 | 3 | 6 | 4 | 2–3 |
| blue | rohirrim_footman | 18 | 7 | 5 | 3 | 5 | 3 | 1–1 |
| red (Isengard) | uruk_berserker | 32 | 11 | 5 | 2 | 6 | 4 | 1–1 |
| red  | uruk_swordsman | 22 | 9 | 5 | 2 | 5 | 4 | 1–1 |
| red  | uruk_crossbow  | 16 | 8 | 3 | 2 | 5 | 4 | 2–3 |

**Forces.** Blue (start): Théoden + Aragorn + Legolas + Gimli + 6
Galadhrim + 6 Rohirrim footmen (= 14). Red (start): 6 berserkers +
12 swordsmen + 6 crossbow (= 24). Reinforcements below.

**Plugin — two scheduled events.**
- Turn 4: berserker plants explosives at the culvert. Narrative
  "The culvert explodes!" + the wall tile at (4, 5) becomes
  passable (demolishes the fort tile via plugin-mutates-board).
- Turn 12: Gandalf arrives at dawn. Spawn Gandalf + Éomer + 8
  Rohirrim cavalry at the east edge (x=15, various y). Narrative
  "Light crests the ridge. Gandalf rides with Éomer!".

**Win conditions.**
- Red wins by eliminating all blues OR bringing 3 uruks through
  into the keep (x=0).
- Blue wins by surviving until turn 15 (after Gandalf arrives) AND
  Théoden alive, OR by killing every uruk.

**Mechanic showcase.** The full plugin system — board mutation,
reinforcement spawning, narrative, multi-condition wins.

---

## 12_five_armies — "Battle under the Lonely Mountain" (The Hobbit)

**Story.** Smaug is dead. The dwarves of Erebor hold their
reclaimed halls, the men of Laketown are starving refugees, the
Wood-elves have come for ancient treasure, and before blood can be
spilled between them, a goblin army with Wargs pours off the
mountain. Three allies against a horde — plus Eagles and Beorn at
the climax.

**Map.** 18 × 14. Lonely Mountain at the north (mountain tiles,
impassable except to certain classes) dominates y=0..2. Erebor's
gate at (9, 2) (fort). Laketown ruins at the east bank of the
running river (x=14). Dale's ruins at (5..8, 5..7). Goblin gate
at the northwest slopes (x=1, y=1).

**Classes.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue (Allies) | thorin           | 38 | 12 | 7 | 3 | 6 | 4 | 1–1 |
| blue | bilbo            | 16 | 5  | 3 | 3 | 8 | 5 | 1–1 | (sneaky; low ATK)
| blue | dwarf_warrior    | 24 | 9  | 6 | 3 | 5 | 3 | 1–1 |
| blue | thranduil        | 30 | 10 | 5 | 5 | 8 | 5 | 1–1 |
| blue | wood_elf_archer  | 16 | 10 | 2 | 3 | 7 | 5 | 2–4 |
| blue | bard             | 24 | 10 | 4 | 3 | 6 | 4 | 2–3 |
| blue | laketown_militia | 14 | 7  | 3 | 2 | 5 | 4 | 1–1 |
| red (Goblins) | bolg             | 34 | 12 | 5 | 2 | 7 | 5 | 1–1 |
| red  | goblin_warrior   | 14 | 7  | 3 | 2 | 5 | 4 | 1–1 |
| red  | goblin_archer    | 12 | 7  | 2 | 2 | 6 | 4 | 2–3 |
| red  | warg_rider       | 22 | 9  | 4 | 2 | 8 | 6 | 1–1 |

**Forces (start).** Blue: Thorin + Bilbo + 6 dwarves + Thranduil +
6 elf archers + Bard + 5 militia (= 21). Red: Bolg + 15 goblin
warriors + 6 goblin archers + 4 warg riders (= 26).

**Plugin reinforcements.**
- Turn 8: a second goblin wave spawns from the northwest (+8 goblin
  warriors). Narrative: "More goblins pour from the mountain."
- Turn 15 (if red hasn't won): Eagles arrive! Plugin spawns 4
  eagle units on the blue side at random north edge tiles.
  Narrative: "The Eagles! The Eagles are coming!"
- Turn 18 (if red still in play): Beorn the bear spawns — one
  very strong unit at x=9, y=12. Narrative: "Beorn in bear form
  bursts through the battle lines!"

**Classes for reinforcements.**

| Team | Class | HP | ATK | DEF | RES | SPD | MOVE | RNG |
|------|-------|----|----|----|----|----|------|-----|
| blue | eagle           | 26 | 9 | 5 | 3 | 10 | 8 | 1–1 | (can_enter_mountain)
| blue | beorn_bear      | 48 | 16 | 8 | 3 | 7 | 5 | 1–1 |

**Win conditions.**
- Blue loses if Thorin dies (tragic — historical/novel outcome).
- Blue wins by eliminating Bolg + all wargs.
- Red wins by taking Erebor's gate (fort-seize at (9, 2)).

**Mechanic showcase.** Multi-wave reinforcements with scheduled
narrative, plus class-override `can_enter_mountain: true` on Eagles.
The biggest combined-arms spectacle of the ten.

---

## Implementation order

Roughly by complexity:

1. **Thermopylae** — chokepoint, no plugins. Warmup.
2. **Cannae** — open plain, pure stats + positioning.
3. **Stirling Bridge** — chokepoint + hold_tile.
4. **Kadesh** — chariot mechanics + mutual fort seize.
5. **Troy** — mutual VIPs.
6. **Little Bighorn** — survive-N-turns VIP.
7. **Agincourt** — class_overrides + mud terrain.
8. **Red Cliffs** — fire-spread plugin + narrative.
9. **Helm's Deep** — board-mutation plugin + reinforcements.
10. **Five Armies** — full plugin system, multi-wave.
