from __future__ import annotations

from pathlib import Path

import release_readiness_ui_tooltips_packet4_corrected_preview as base


base.SOURCE = "release_readiness_ui_tooltips_packet5_corrected_preview_v1"
base.PACKET_JSONL = Path("reports/20260703_140007_775717_release_readiness_ui_tooltips_plain_light_human_packet.jsonl")
base.SEGMENT_STATE_RUN_ID = 576
base.LEDGER_RUN_ID = 77
base.CORRECTIONS = {
    52828: "Você envia envenenadores especialistas para matar seletivamente os predadores fora de controle com iscas.",
    60150: "Você deve ser o defensor principal em uma [war|El] que não use os [casus_bellis|El] $fp2_border_raid$ nem $county_struggle_cb$",
    62986: "Trazer talentos estrangeiros para sua [court|lE] ao custo de escandalizar seus [vassals|lE]",
    63033: "Você escolherá uma fé zoroastriana praticada atualmente em pelo menos um [county|lE] do seu [realm|lE] para adorar em [secret|lE]",
    143987: "O progresso base e o impacto de [skill|lE] em [councillor_tasks|lE] aumentam em #P +10%#! para membros da [house|lE] que sirvam em seu [council|lE]",
    155528: "Sua [stewardship|lE] alta permite que você drene o [control|lE] dos [counties|lE] do seu [liege|lE]",
    155529: "Sua [stewardship|lE] moderada permite que você drene o [control|lE] de um #EMP único#! [county|lE] do seu [liege|lE]",
    163030: "O desenvolvimento do condado é baixo demais, ou ele já tem um espaço para edifício especial",
}


if __name__ == "__main__":
    base.main()
