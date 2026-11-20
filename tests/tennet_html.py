"""Ingekorte echte TenneT-tabelpagina (publications.tennet.eu, pagina 90035), zoals op 20-11-2026 opgehaald."""
KOP = ('<input type="hidden" name="p_flow_step_id" value="90035" id="pFlowStepId" />'
       '<a-date-picker id="P90035_PTU_DATE_LOC"  change-month="true" format="DD-MM-YYYY" valid-example="20-11-2026" value="{datum}" year-selection-range="5">'
       '<input  aria-haspopup="dialog" class="apex-item-text&#x20;apex-item-datepicker" name="P90035_PTU_DATE_LOC" size="30" type="text" value="{datum}"></a-date-picker>')
TH = ('<thead><tr><th class="t-Report-colHead" align="right" id="PTU">ISP</th><th class="t-Report-colHead" align="left" id="PERIODE">Period</th>'
      '<th class="t-Report-colHead" align="left" id="IND_INCIDENT_RESERVE_UP">Incident reserve up</th><th class="t-Report-colHead" align="left" id="IND_INCIDENT_RESERVE_DOWN">Incident reserve down</th>'
      '<th class="t-Report-colHead" align="right" id="PRICE_DISPATCH_UP">Upward regulation (EUR/MWh)</th><th class="t-Report-colHead" align="right" id="PRICE_DISPATCH_DOWN">Downward regulation (EUR/MWh)</th>'
      '<th class="t-Report-colHead" align="right" id="PRICE_SHORTAGE">Shortage (EUR/MWh)</th><th class="t-Report-colHead" align="right" id="PRICE_SURPLUS">Surplus (EUR/MWh)</th>'
      '<th class="t-Report-colHead" align="right" id="REGULATION_STATE">Regulation state</th></tr></thead>')


def rij(isp, periode, op, af, tekort, overschot, rs):
    c = lambda h, v, al=' align="right"': f'<td class="t-Report-cell"{al} headers="{h}">{v}</td>'
    return ('<tr>' + c("PTU", isp) + c("PERIODE", periode, "") + c("IND_INCIDENT_RESERVE_UP", "", "") + c("IND_INCIDENT_RESERVE_DOWN", "", "")
            + c("PRICE_DISPATCH_UP", op) + c("PRICE_DISPATCH_DOWN", af) + c("PRICE_SHORTAGE", tekort) + c("PRICE_SURPLUS", overschot)
            + c("REGULATION_STATE", rs) + '</tr>\n')


def pagina(datum: str, rijen: list) -> str:
    if not rijen:  # dag nog niet gepubliceerd: geen tabel
        return '<html><body>' + KOP.format(datum=datum) + '<div>Data refeshed at: </div></body></html>'
    return ('<html><body>' + KOP.format(datum=datum) + '<table class="t-Report-report" id="report_table_R93369768093509395" aria-label="atr_06_table">\n'
            + TH + '\n<tbody>\n' + ''.join(rij(*r) for r in rijen) + '      </tbody>\n    </table></body></html>')


# Echte rijen van 15-06-2026 (ISP 1, 2, 3, 40, 60, 96).
JUNI15 = [(1, "00:00-00:15", "108.17", "50", "108.17", "50", "2"), (2, "00:15-00:30", "108.17", "24.4", "108.17", "24.4", "2"),
          (3, "00:30-00:45", "119.6", "", "119.6", "119.6", "1"), (40, "09:45-10:00", "3500", "", "3500", "3500", "1"),
          (60, "14:45-15:00", "", "-.68", "-.68", "-.68", "-1"), (96, "23:45-00:00", "139", "93.73", "139", "93.73", "2")]
