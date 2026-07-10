from __future__ import annotations

from pathlib import Path

import release_readiness_ui_tooltips_packet4_corrected_preview as base


base.SOURCE = "release_readiness_ui_tooltips_final6_corrected_preview_v1"
base.PACKET_JSONL = Path("reports/20260703_142012_800811_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
base.SEGMENT_STATE_RUN_ID = 577
base.LEDGER_RUN_ID = 77
base.CORRECTIONS = {
    50666: "@alert_icon! #alert_trial Se você morrer antes que um herdeiro seja designado, sua dinastia pode acabar!#!",
    69330: "Visite a [realm_capital|lE] do [son_of_heaven|E] ou do [minister|lE] e apresente uma questão urgente que seu [movement|lE] queira mudar",
    160251: "#WEAK Este desafio é, no geral, um pouco mais fácil do que testar [stewardship|lE]#!",
    160493: "@alert_icon! #alert_trial Esta opção é forçada porque você alcançou o melhor resultado que pode obter com o número de falhas que acumulou#!",
    60232: "Você e seu oponente devem ter [domains|lE] adjacentes que não sejam suas [realm_capitals|lE]",
}


if __name__ == "__main__":
    base.main()
