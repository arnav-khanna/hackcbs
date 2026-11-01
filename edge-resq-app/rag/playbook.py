"""Built-in, offline emergency playbooks.

Short, standard public-safety steps (evacuate, drop-cover-hold-on, avoid
floodwater, ...) returned whenever retrieved reports contain no applicable
guidance or the local model is unavailable. They are static text: they cannot
be hallucinated, need no network, and are returned instantly.

Review the wording and the numbers below with your local emergency-management
authority before production use. Numbers default to India.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Phase = Literal["active", "aftermath"]

EMERGENCY_NUMBERS: tuple[str, ...] = (
    "112 - national emergency number (police, fire, ambulance)",
    "101 - fire service",
    "108 - ambulance",
)

DISCLAIMER = (
    "General safety guidance only. Follow instructions from emergency services "
    "and local authorities. If anyone is in immediate danger, call 112."
)


@dataclass(frozen=True)
class Playbook:
    key: str
    phase: Phase
    headline: str
    do_now: tuple[str, ...]
    avoid: tuple[str, ...]
    next_steps: tuple[str, ...] = ()


_FIRE_ACTIVE = Playbook(
    "fire", "active",
    "Get out and call for help. Do not stop for belongings.",
    do_now=(
        "Get out now. Leave by the nearest safe exit and do not stop for belongings.",
        "If there is smoke, stay low where the air is clearer and cover your nose and mouth with a wet cloth.",
        "Before opening any door, touch it with the back of your hand. If it is hot, keep it closed and use another way out.",
        "Once outside, move well away from the building and call 101 or 112. Give the address and say whether anyone is still inside.",
        "If your clothes catch fire: stop, drop to the ground, cover your face with your hands and roll until the flames are out.",
        "If you cannot get out: close the door, block the gaps with wet cloth, go to a window, signal for help and call 112 with your exact location.",
    ),
    avoid=(
        "Do not use lifts.",
        "Do not go back inside for people, pets or belongings. Tell firefighters who is still inside.",
        "Do not throw water on an oil fire or an electrical fire.",
    ),
    next_steps=(
        "Stay outside until firefighters say the building is safe.",
        "Get medical care for any burns or breathing problems, even if they seem minor.",
    ),
)

_FIRE_AFTERMATH = Playbook(
    "fire", "aftermath",
    "The fire is over. Focus on safety and shelter first.",
    do_now=(
        "Check yourself and everyone with you for burns, cough, breathlessness or dizziness. Call 108 or 112 for anyone who has them.",
        "For a burn, cool it under cool running water for about 20 minutes and cover it loosely with a clean cloth. Do not use ice, butter or ointment. Get medical help for large, deep or face burns.",
        "Stay out of the damaged building until fire officials say it is safe. Weakened structures can collapse and hidden embers can reignite.",
        "If you have nowhere safe to stay, call 112 or your local administration for emergency shelter, food and clothing.",
        "Tell a relative or friend where you are and that you are safe.",
    ),
    avoid=(
        "Do not touch damaged wiring, gas lines or appliances.",
        "Do not eat food or drink water that was exposed to heat, smoke or firefighting water.",
    ),
    next_steps=(
        "Photograph the damage from outside the building and keep any records you have for insurance or compensation claims.",
        "Ask your local administration how to replace lost identity papers and other documents.",
        "Losing everything is a shock. Stay with people you trust and ask for help.",
    ),
)

_EARTHQUAKE = Playbook(
    "earthquake", "active",
    "Drop, cover and hold on. After the shaking stops, get to open ground and call for help.",
    do_now=(
        "If you are inside: DROP to your hands and knees, take COVER under a sturdy table or against an inside wall, protect your head and neck, and HOLD ON until the shaking stops.",
        "If you are outside: move to an open area away from buildings, trees, street lights and power lines, then drop down.",
        "If you are driving: stop in a clear area away from bridges, overpasses and power lines, and stay in the vehicle until the shaking stops.",
        "When the shaking stops, check yourself and others for injuries. Leave a damaged building carefully by the stairs and move to open ground.",
        "Expect aftershocks. Drop, cover and hold on each time.",
        "If you are trapped under debris: cover your mouth with a cloth, tap on a pipe or wall or use a whistle so rescuers can find you, and shout only as a last resort because it makes you breathe in dust.",
        "If you are near the coast and the shaking was strong or long, move to high ground immediately without waiting for a warning.",
        "Call 112 and give your exact location. Send an SMS if calls will not connect.",
    ),
    avoid=(
        "Do not run outside or use lifts while the ground is shaking.",
        "Do not light matches, lighters or candles, and do not use electrical switches, if you smell gas.",
        "Do not re-enter a damaged building.",
        "Stay away from damaged walls, balconies, windows and anything that could fall.",
    ),
    next_steps=(
        "If you smell gas, leave the area and tell the authorities.",
        "Listen to a radio or official alerts for instructions.",
    ),
)

_FLOOD = Playbook(
    "flood", "active",
    "Move to higher ground now. Do not wait for an official warning if water is rising.",
    do_now=(
        "Move to higher ground now. If water is rising, do not wait for an official warning.",
        "If you cannot leave, go to the highest floor or the roof and signal for help with a bright cloth, torch or whistle. Do not shelter in a closed attic.",
        "Call 112 and give your exact location and how many people are with you.",
        "Keep children and older people with you. Take only what you can grab in seconds: phone, medicines, documents.",
        "Switch off the main power only if you can reach the switch without standing in water.",
        "Never walk or drive through moving floodwater. About 15 cm can knock you off your feet and about 30 cm can float a car. If your vehicle stalls in rising water, leave it and move to higher ground.",
    ),
    avoid=(
        "Do not touch electrical equipment or fallen power lines when you are wet or standing in water.",
        "Do not drink or cook with floodwater or tap water until the authorities say it is safe.",
        "Do not return home until the authorities say it is safe.",
    ),
    next_steps=(
        "After the water recedes, do not use electrical appliances that got wet until they are checked.",
        "Photograph the damage for insurance or compensation claims.",
    ),
)

_CYCLONE = Playbook(
    "cyclone", "active",
    "Shelter in a strong building away from windows, or evacuate if ordered.",
    do_now=(
        "Follow evacuation orders immediately. If you are in a coastal or low-lying area, or in a weak house, go to the nearest cyclone shelter or sturdy building now.",
        "Stay in the strongest part of a sturdy building, in a small interior room away from windows, doors and glass.",
        "Keep water, a torch, medicines, a charged phone, a power bank and a battery radio with you, and listen to official alerts.",
        "If the wind suddenly drops, stay inside. It may be the eye of the storm and strong winds will return, often from the opposite direction.",
        "If water starts entering the building, move to an upper floor.",
        "Call 112 if anyone is injured or trapped and give your exact location.",
    ),
    avoid=(
        "Do not go to the shore, harbour or riverbank.",
        "Do not go outside to check damage until officials give the all-clear.",
        "Do not touch fallen power lines or go near broken poles and trees.",
    ),
    next_steps=(
        "Stay away from flooded roads and damaged structures after the storm passes.",
    ),
)

_GLOF = Playbook(
    "glacial_lake_outburst", "active",
    "Climb to high ground on the valley side now. Do not wait for confirmation.",
    do_now=(
        "Move immediately to high ground on the valley side, well above the river. Do not wait for confirmation.",
        "Warn people nearby as you go and take children and older people with you.",
        "Stay away from the river, its banks and low-lying areas, including bridges and crossings that may be overtopped or washed away.",
        "Call 112 or the district disaster control room and report your location and what you see.",
        "More surges can follow. Stay on high ground until the authorities say it is safe.",
    ),
    avoid=(
        "Do not go to the river to watch or to collect belongings.",
        "Do not camp, park or stay in the river valley or on the riverbed.",
        "Do not try to cross flowing or muddy water.",
    ),
)

_DROUGHT = Playbook(
    "drought", "active",
    "Protect drinking water first and ask the local administration for supply.",
    do_now=(
        "Keep your water for drinking and cooking first. Children, older people, pregnant women and sick people need it most.",
        "Contact your local administration, panchayat or ward office about tanker supply and relief, and register your household.",
        "Treat water from unfamiliar or stagnant sources before drinking: bring it to a rolling boil for at least one minute.",
        "Watch for dehydration: dizziness, very dark or very little urine, confusion or extreme weakness. Call 108 or 112 if someone has these signs, especially a child or an older person.",
        "Keep livestock near any safe water source and report shortages to the local veterinary or administration office.",
    ),
    avoid=(
        "Do not drink untreated water from dirty, stagnant or unknown sources.",
        "Do not use scarce water for washing vehicles or other non-essential uses.",
        "Avoid heavy work in the midday heat.",
    ),
)

_GENERAL = Playbook(
    "general", "active",
    "If anyone is in immediate danger, call 112 now.",
    do_now=(
        "Call 112 now if anyone is in immediate danger. If calls will not connect, send an SMS or ask someone nearby to call for you.",
        "Move away from the danger to the nearest open, safe place and keep children and others with you.",
        "Tell the operator exactly where you are (address or landmark), what happened, how many people are affected and whether anyone is injured or trapped.",
        "Stay on the line and follow the operator's instructions. Keep your phone charged and your location sharing on.",
        "Press firmly on heavy bleeding with a clean cloth and keep the injured person still until help arrives.",
    ),
    avoid=(
        "Do not go back into a danger area to collect belongings.",
        "Do not move an injured person unless they are in immediate danger.",
    ),
    next_steps=(
        "Message a family member or friend with your location.",
    ),
)

_PLAYBOOKS: dict[tuple[str, Phase], Playbook] = {
    (p.key, p.phase): p
    for p in (_FIRE_ACTIVE, _FIRE_AFTERMATH, _EARTHQUAKE, _FLOOD, _CYCLONE, _GLOF, _DROUGHT, _GENERAL)
}


def get_playbook(incident_type: str, phase: Phase = "active") -> Playbook:
    """Return the playbook for a type and phase, falling back to active, then general."""
    return (
        _PLAYBOOKS.get((incident_type, phase))
        or _PLAYBOOKS.get((incident_type, "active"))
        or _GENERAL
    )