import json

from firma.quota import Sonda, para_datetime, resumir, salvar


def test_reset_em_segundos_e_milissegundos_dao_o_mesmo_horario():
    assert para_datetime(1_788_465_600) == para_datetime(1_788_465_600_000)
    assert para_datetime(None) is None


def test_sem_utilizacao_manda_estimar_por_tokens():
    sonda = Sonda(fonte_credencial="none", eventos=[{"status": "allowed", "rateLimitType": "five_hour"}])
    linhas, veredito = resumir(sonda)
    assert any("tipo=five_hour" in l for l in linhas)
    assert any("estimar" in v for v in veredito)


def test_com_utilizacao_calcula_direto():
    sonda = Sonda(
        fonte_credencial="none",
        eventos=[{"status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.82, "resetsAt": 1_788_465_600}],
    )
    linhas, veredito = resumir(sonda)
    assert any("utilização=82%" in l for l in linhas)
    assert any("direto" in v for v in veredito)


def test_janelas_unificadas_contam_como_utilizacao():
    sonda = Sonda(fonte_credencial="none", eventos=[{"status": "allowed", "unifiedWindows": [{"type": "five_hour"}]}])
    _, veredito = resumir(sonda)
    assert any("direto" in v for v in veredito)


def test_chave_de_api_gera_alerta():
    _, veredito = resumir(Sonda(fonte_credencial="ANTHROPIC_API_KEY"))
    assert any("ATENÇÃO" in v for v in veredito)


def test_salvar_grava_json(tmp_path):
    caminho = salvar(Sonda(fonte_credencial="none"), tmp_path)
    assert json.loads(caminho.read_text(encoding="utf-8"))["fonte_credencial"] == "none"
