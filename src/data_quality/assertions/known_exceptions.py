"""Known exceptions: violations a data-quality assertion must not count,
each verified by hand and kept with its reason.

An exception is for data the platform cannot resolve because the source
itself contradicts itself, never for a pipeline bug. Each entry names one
record; anything NOT listed still fails its assertion, so a new case is
never hidden. Re-verify an entry before extending a list: when a listed
record stops violating, delete it.
"""
from __future__ import annotations

# grain.notice_belongs_to_one_contract, verified 2026-09-29 (48 notices; 8
# pruned the same day once the roll-up fix let adopt fold them: 40 left).
# After the adopt rule became "merge unless an outside award explicitly
# disagrees" (fontem-neo4j-sink #176), these are the notices still hanging
# off two contracts, and in each the buyer's own notice is what disagrees:
SAME_BUYER_TWO_IDS = (
    "same buyer under two authority ids (e.g. EUSPA and its former name "
    "GSA): the back-link is real, but nothing carries over by id, so the "
    "link step marks it doubtful and the adopt step keeps the contracts apart"
)
CROSS_PROCEDURE_BACK_LINK = (
    "the notice's back-link names an award of a different eForms procedure "
    "that has an award of its own (e.g. a Banyoles music-school modification "
    "naming the museum-supply award 781096-2024): a buyer typo or a "
    "framework call-off, which the data cannot tell apart"
)

NOTICE_ON_TWO_CONTRACTS: dict[str, str] = {
    # Új pótdíjkezelő rendszer fejlesztése, támogatása
    "00077083-1053-4f1e-ae74-df6a2c910a78": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "1963d821-21dc-43ca-9e62-f146bcd8fd1d": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "2a687fe5-ad48-418e-9d98-081c129f8b75": SAME_BUYER_TWO_IDS,
    # Új pótdíjkezelő rendszer fejlesztése, támogatása
    "3de5aea5-a232-41b5-a0ab-984dcc556e2c": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "3df8ff7a-619c-4a91-81a2-1759423598be": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "4175386d-1519-4df8-a96c-d301fe901ed4": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "58dc7f4c-1518-49ec-99d1-099a735b3538": SAME_BUYER_TWO_IDS,
    # Προμήθεια αντλιών και ανταλλακτικών αντλιών / φυ
    "5e8ca235-35ee-4e0c-968d-295ca80db353": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "6ec1a18d-8d8b-4b54-aea9-bd472f9fcb01": SAME_BUYER_TWO_IDS,
    # Galileo 2nd Generation Non-PRS Test User Receive
    "6ff0788c-67dc-413b-b6ea-87e08eb0e0e9": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "761b2aa0-1d66-4518-b8f3-066275a05d7b": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "7afc1063-9952-4d59-b946-e0faa269a508": SAME_BUYER_TWO_IDS,
    # Servicios de Consultoría y Asistencia Técnica pa
    "93a18fa9-906d-45b4-b8af-65a2a079c115": SAME_BUYER_TWO_IDS,
    # „Доставка на санитарни автомобили, разделена на
    "9cf1900d-552c-4019-838a-eb2773520c43": SAME_BUYER_TWO_IDS,
    # Új pótdíjkezelő rendszer fejlesztése, támogatása
    "ab13acdb-db46-4415-8067-3912b2e5ba44": SAME_BUYER_TWO_IDS,
    # Przedmiotem zamówienia jest wykonanie rob bud. w
    "b2bf47df-7bd6-4a09-a829-b7e70620a9c7": SAME_BUYER_TWO_IDS,
    # ΔΙΑΧΕΙΡΙΣΗ ΟΓΚΩΔΩΝ ΑΠΟΒΛΗΤΩΝ ΟΙΚΙΑΚΗΣ ΧΡΗΣΗΣ ΠΟΥ
    "d4dd9622-8b83-4344-a0c2-5b97cc11853a": SAME_BUYER_TWO_IDS,
    # Wykonywanie usług z zakresu gospodarki leśnej na
    "dc4596de-5af1-46dd-91d6-cbe41b7988ca": SAME_BUYER_TWO_IDS,
    # Galileo Ground Mission Segment and Security Faci
    "e8769653-8e23-4d67-ab32-22bd4bf21a63": SAME_BUYER_TWO_IDS,
    # „Доставка, гаранционна поддръжка и гаранционно о
    "ef670102-f2c5-46b1-b16b-35309f19d7ed": SAME_BUYER_TWO_IDS,
    # Ejecución de las obras del proyecto constructivo
    "f19c0da3-2c0f-406a-ad4a-460f7fa3049e": SAME_BUYER_TWO_IDS,
    # Poprawa układu drogowego w dzielnicy Białołęka –
    "f2b94920-8518-45c1-8279-6e7a24de50df": SAME_BUYER_TWO_IDS,
    # Empreitada de Requalificação da Escola D. José I
    "0216f9e2-ee43-4137-9bd2-adc8ecbcd212": CROSS_PROCEDURE_BACK_LINK,
    # „Poprawa efektywności energetycznej budynków uży
    "0f04d03e-fc85-48db-893f-f293dd93b061": CROSS_PROCEDURE_BACK_LINK,
    # SUBMINISTRO Y SERVICIO DE MUSEOGRAFIA MACB
    "2ffcacbb-7b4a-4b7e-9152-454a3ba72370": CROSS_PROCEDURE_BACK_LINK,
    # Empreitada de Requalificação da Escola D. José I
    "46c29f04-1717-405a-a846-49fa746235b0": CROSS_PROCEDURE_BACK_LINK,
    # Smlouva o veřejných službách v přepravě cestujíc
    "6aaae653-c459-426e-abd9-c4ffccbbf88d": CROSS_PROCEDURE_BACK_LINK,
    # Rozbudowa systemu HIS o nowe moduły i integracje
    "71a898de-af03-41cf-a3d7-27e0edfa1cfc": CROSS_PROCEDURE_BACK_LINK,
    # Operation and Maintenance of Laser Systems - Con
    "9fc51c1a-d79e-499b-8cc2-35c731628c93": CROSS_PROCEDURE_BACK_LINK,
    # Υποστήριξη του ΕΚΚΑ στην άμεση τηλεφωνική παροχή
    "a0697979-346d-46bf-8c07-49e09763fa9b": CROSS_PROCEDURE_BACK_LINK,
    # SUBMINISTRO Y SERVICIO DE MUSEOGRAFIA MACB
    "a4afbcf0-6411-4fc9-bceb-0d4eac9890fa": CROSS_PROCEDURE_BACK_LINK,
    # Παροχή υπηρεσιών καθαριότητας των χώρων του Πολυ
    "a4e0f645-b792-44ca-b7c7-cef8cd680d2a": CROSS_PROCEDURE_BACK_LINK,
    # SUBMINISTRO Y SERVICIO DE MUSEOGRAFIA MACB
    "b2dd3b02-cbb3-4e32-b2a1-8b8d1bdee3f2": CROSS_PROCEDURE_BACK_LINK,
    # Usługi konserwacji i napraw awaryjnych systemów
    "b3fd823c-910d-4291-bdec-5968f33fb13e": CROSS_PROCEDURE_BACK_LINK,
    # Zimowe bieżące utrzymanie dróg, chodników i park
    "b936bf4a-0787-44b3-9876-50cc9505ddac": CROSS_PROCEDURE_BACK_LINK,
    # Remont i przebudowa budynku biurowo-laboratoryjn
    "bb53a49b-c5f7-4b82-8d5d-b46f2a8f72dd": CROSS_PROCEDURE_BACK_LINK,
    # 25 E 038 - Planungsleistungen Ingenieurbauwerke
    "c91d6261-97dc-4043-ac86-f84c0b91f911": CROSS_PROCEDURE_BACK_LINK,
    # servicio de mantenimiento integral a todo riesgo
    "de032ba7-3d73-4fcf-91fb-99bada5b1916": CROSS_PROCEDURE_BACK_LINK,
    # FORNITURA CASSONETTI E BIDONI
    "eb97be6e-4ad6-41c5-aef2-a85ab2632f36": CROSS_PROCEDURE_BACK_LINK,
    # Zakup angiografu z wyposażeniem
    "f359cc08-7d10-45df-992c-94d3079148d5": CROSS_PROCEDURE_BACK_LINK,
}


def cypher_list(ids) -> str:
    """A Cypher list literal of ids. Ids are TED notice ids (UUIDs or
    publication numbers); anything else is refused rather than quoted."""
    out = []
    for i in sorted(ids):
        if not all(ch.isalnum() or ch in "-/ " for ch in i):
            raise ValueError(f"not an id: {i!r}")
        out.append(f"'{i}'")
    return "[" + ", ".join(out) + "]"
