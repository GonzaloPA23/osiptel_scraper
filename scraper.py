# -*- coding: utf-8 -*-
"""
API OSIPTEL — Scraper como servicio REST
========================================
Endpoint: POST /consultar  → { "ruc": "20123456789" }
Respuesta: JSON con conteos por operador

Uso local:
    uvicorn scraper:app --host 0.0.0.0 --port 8000 --reload

Deploy Railway:
    Usar Dockerfile incluido
"""

import os
import re
import time
import random
import subprocess
from collections import Counter
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# ===================== DETECTAR ENTORNO =====================
IS_LINUX = os.name != "nt"  # True en Railway/Linux, False en Windows local

# ===================== APP =====================
app = FastAPI(
    title="OSIPTEL Scraper API",
    description="Consulta líneas telefónicas por RUC en OSIPTEL",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== MODELOS =====================
class ConsultaRequest(BaseModel):
    ruc: str

    @field_validator("ruc")
    @classmethod
    def validar_ruc(cls, v):
        ruc_limpio = re.sub(r"\D", "", v)
        if len(ruc_limpio) != 11:
            raise ValueError("El RUC debe tener exactamente 11 dígitos")
        return ruc_limpio

class ConsultaResponse(BaseModel):
    ruc: str
    estado: str
    mensaje: str
    q_entel: int
    q_claro: int
    q_movistar: int
    q_bitel: int
    q_wom: int
    q_otros: int
    q_total: int

# ===================== DRIVER MANAGER =====================
URL = "https://checatuslineas.osiptel.gob.pe/"
TIMEOUT = 30

class DriverManager:
    def __init__(self):
        self.driver = None

    def _safe_quit(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass

        if not IS_LINUX:
            # Solo en Windows matar procesos manualmente
            for proc in ["chromedriver.exe", "chrome.exe"]:
                subprocess.run(
                    ["taskkill", "/F", "/IM", proc, "/T"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
        time.sleep(2)
        self.driver = None

    def _build_options(self) -> Options:
        opts = Options()

        if IS_LINUX:
            # ✅ Modo headless para Railway/Linux (sin interfaz gráfica)
            opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--window-size=1920,1080")
            opts.add_argument("--disable-setuid-sandbox")
            opts.add_argument("--disable-extensions")
            opts.add_argument("--dns-prefetch-disable")
            # Usar Chrome instalado en el sistema (Docker)
            opts.binary_location = "/usr/bin/google-chrome"
        else:
            # ✅ Modo visible para desarrollo local en Windows
            opts.add_argument("--start-maximized")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")

        opts.add_argument("--lang=es-PE")
        opts.add_argument("--disable-features=NetworkService,NetworkServiceInProcess")
        opts.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.page_load_strategy = "eager"
        return opts

    def build(self):
        self._safe_quit()
        opts = self._build_options()

        for intento in range(5):
            try:
                print(f"🧠 Iniciando Chrome (intento {intento+1}) | Linux={IS_LINUX}...")

                if IS_LINUX:
                    # En Linux/Railway usar chromedriver del sistema
                    service = Service("/usr/bin/chromedriver")
                else:
                    # En Windows usar webdriver-manager
                    service = Service(ChromeDriverManager().install())

                drv = webdriver.Chrome(service=service, options=opts)
                drv.set_page_load_timeout(60)
                drv.implicitly_wait(2)
                drv.execute_script("return navigator.userAgent;")
                self.driver = drv
                print(f"🟢 Chrome OK | Headless={IS_LINUX} | Session: {drv.session_id}")
                return drv
            except Exception as e:
                print(f"⚠️ Error iniciando Chrome (intento {intento+1}): {e}")
                self._safe_quit()
                time.sleep(5)

        raise RuntimeError("No se pudo iniciar Chrome tras 5 intentos")

    def get(self):
        if self.driver is None:
            self.build()
        try:
            self.driver.execute_script("return 1;")
            return self.driver
        except Exception:
            print("⚠️ Driver caído, reiniciando...")
            self.build()
            return self.driver

    def go_home(self):
        drv = self.get()
        for intento in range(8):
            try:
                drv.get(URL)
                WebDriverWait(drv, TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "IdTipoDoc"))
                )
                print("✅ OSIPTEL cargado.")
                time.sleep(random.uniform(1, 2))
                return
            except Exception as e:
                print(f"⚠️ Fallo cargando OSIPTEL (intento {intento+1}): {e}")
                if intento >= 3:
                    self.build()
                time.sleep(random.uniform(4, 7))
        raise RuntimeError("No se pudo cargar OSIPTEL")

    def reiniciar(self):
        print("♻️ Reiniciando Chrome...")
        self.build()
        self.go_home()


driver_mgr = DriverManager()

# ===================== LÓGICA SCRAPER =====================
def normalize_operator(op: str) -> str:
    op = op.upper()
    if "ENTEL" in op:                          return "ENTEL"
    if "AMERICA MOVIL" in op or "CLARO" in op: return "CLARO"
    if "MOVISTAR" in op or "TELEFONICA" in op: return "MOVISTAR"
    if "BITEL" in op or "VIETTEL" in op:       return "BITEL"
    if "WOM" in op:                            return "WOM"
    return "OTROS"

def esperar_tabla(driver, max_espera=30) -> bool:
    start = time.time()
    while time.time() - start < max_espera:
        if "La consulta no se pudo procesar" in driver.page_source:
            return False
        try:
            spinner = driver.find_element(By.XPATH, "//*[contains(text(), 'Procesando')]")
            if spinner.is_displayed():
                time.sleep(0.5)
                continue
        except Exception:
            pass
        rows = driver.find_elements(By.CSS_SELECTOR, "#GridConsulta tbody tr")
        if rows and any(r.text.strip() for r in rows):
            return True
        if "No se encontraron resultados" in driver.page_source:
            return False
        time.sleep(0.5)
    return False

def collect_counts(driver) -> Counter:
    counts = Counter()
    rows = driver.find_elements(By.CSS_SELECTOR, "#GridConsulta tbody tr")
    for r in rows:
        tds = r.find_elements(By.TAG_NAME, "td")
        if len(tds) >= 3:
            op = normalize_operator(tds[-1].text)
            counts[op] += 1
    return counts

def paginate_all(driver) -> Dict[str, int]:
    counts = Counter()
    try:
        Select(driver.find_element(By.NAME, "GridConsulta_length")).select_by_value("100")
        time.sleep(random.uniform(2, 3))
    except Exception:
        pass

    page = 1
    while True:
        esperar_tabla(driver, 10)
        counts.update(collect_counts(driver))
        print(f"   📄 Página {page} | acumulado: {sum(counts.values())}")
        page += 1
        try:
            next_btn = driver.find_element(By.ID, "GridConsulta_next")
            if "disabled" in (next_btn.get_attribute("class") or "").lower():
                break
            link = next_btn.find_element(By.TAG_NAME, "a")
            driver.execute_script("arguments[0].click();", link)
            time.sleep(random.uniform(1.5, 2))
        except NoSuchElementException:
            break
    return dict(counts)

def esta_en_osiptel(drv) -> bool:
    """Verifica si OSIPTEL está cargado correctamente."""
    try:
        drv.find_element(By.ID, "IdTipoDoc")
        return True
    except Exception:
        return False

def scrape_ruc(ruc: str) -> Dict:
    for intento in range(1, 4):
        try:
            drv = driver_mgr.get()
            print(f"[{ruc}] Intento {intento}...")

            # ✅ Si OSIPTEL no está cargado, cargarlo primero
            if not esta_en_osiptel(drv):
                print("   🌐 Cargando OSIPTEL antes de consultar...")
                driver_mgr.go_home()
                drv = driver_mgr.get()

            Select(drv.find_element(By.ID, "IdTipoDoc")).select_by_value("2")
            box = drv.find_element(By.ID, "NumeroDocumento")
            box.clear()
            box.send_keys(ruc)
            drv.find_element(By.ID, "btnBuscar").click()
            time.sleep(random.uniform(2, 3))

            if not esperar_tabla(drv, 25):
                if intento < 3:
                    driver_mgr.reiniciar()
                    continue
                return {"estado": "VACIO", "mensaje": "Sin resultados", "counts": {}}

            counts = paginate_all(drv)
            total = sum(counts.values())
            return {
                "estado": "OK" if total > 0 else "VACIO",
                "mensaje": f"{total} líneas encontradas" if total > 0 else "Sin resultados",
                "counts": counts
            }

        except Exception as e:
            print(f"   ⚠ Error intento {intento}: {e}")
            if intento < 3:
                driver_mgr.reiniciar()
            else:
                return {"estado": "ERROR", "mensaje": str(e)[:120], "counts": {}}

    return {"estado": "ERROR", "mensaje": "Falló tras 3 intentos", "counts": {}}

# ===================== ENDPOINTS =====================
@app.on_event("startup")
async def startup():
    print("🚀 API OSIPTEL lista. Chrome iniciara en la primera consulta.")

@app.get("/warmup")
def warmup():
    try:
        driver_mgr.build()
        driver_mgr.go_home()
        return {"mensaje": "Chrome listo y OSIPTEL cargado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.on_event("shutdown")
async def shutdown():
    driver_mgr._safe_quit()

@app.get("/")
def root():
    return {"mensaje": "API OSIPTEL activa", "version": "1.0.0", "entorno": "linux" if IS_LINUX else "windows"}

@app.get("/health")
def health():
    try:
        driver_mgr.get().execute_script("return 1;")
        chrome_ok = True
    except Exception:
        chrome_ok = False
    return {"api": "ok", "chrome": "ok" if chrome_ok else "caído"}

@app.post("/consultar", response_model=ConsultaResponse)
def consultar(body: ConsultaRequest):
    ruc = body.ruc
    print(f"\n🔍 Consultando RUC: {ruc}")
    resultado = scrape_ruc(ruc)
    counts = resultado.get("counts", {})
    return ConsultaResponse(
        ruc=ruc,
        estado=resultado["estado"],
        mensaje=resultado["mensaje"],
        q_entel=counts.get("ENTEL", 0),
        q_claro=counts.get("CLARO", 0),
        q_movistar=counts.get("MOVISTAR", 0),
        q_bitel=counts.get("BITEL", 0),
        q_wom=counts.get("WOM", 0),
        q_otros=counts.get("OTROS", 0),
        q_total=sum(counts.values()),
    )

@app.post("/reiniciar-chrome")
def reiniciar_chrome():
    try:
        driver_mgr.reiniciar()
        return {"mensaje": "Chrome reiniciado correctamente"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

