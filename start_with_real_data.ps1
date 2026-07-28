# Script para iniciar o servidor e o agente com dados reais da máquina

Write-Host "================================================" -ForegroundColor Cyan
Write-Host "  ITAM - Iniciando com dados REAIS da máquina" -ForegroundColor Green
Write-Host "================================================`n" -ForegroundColor Cyan

# Verificar se Python está instalado
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Host "❌ Python não encontrado. Por favor, instale Python 3.8+" -ForegroundColor Red
    exit 1
}

Write-Host "✅ Python encontrado: $(python --version)`n" -ForegroundColor Green

# Instalar dependências do agent
Write-Host "[1/4] Instalando dependências do agent..." -ForegroundColor Yellow
pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Erro ao instalar dependências do agent" -ForegroundColor Red
    exit 1
}
Write-Host "✅ Dependências do agent instaladas`n" -ForegroundColor Green

# Instalar dependências do servidor
Write-Host "[2/4] Instalando dependências do servidor..." -ForegroundColor Yellow
pip install -q fastapi uvicorn[standard]
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Erro ao instalar dependências do servidor" -ForegroundColor Red
    exit 1
}
Write-Host "✅ Dependências do servidor instaladas`n" -ForegroundColor Green

# Iniciar o servidor em background
Write-Host "[3/4] Iniciando servidor FastAPI..." -ForegroundColor Yellow
$serverProcess = Start-Process python -ArgumentList "-m", "uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8000" -PassThru -NoNewWindow
Start-Sleep -Seconds 2
Write-Host "✅ Servidor iniciado (PID: $($serverProcess.Id))`n" -ForegroundColor Green

# Executar o agente para coletar dados
Write-Host "[4/4] Coletando dados da máquina com o agente..." -ForegroundColor Yellow
python agent.py --dry-run | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "⚠️  Aviso: Agente teve algum problema, mas prosseguindo..." -ForegroundColor Yellow
}

# Enviar dados para o servidor
Write-Host "Enviando dados coletados para o servidor..." -ForegroundColor Yellow
python agent.py
Start-Sleep -Seconds 1

Write-Host "`n================================================" -ForegroundColor Cyan
Write-Host "✅ TUDO PRONTO!" -ForegroundColor Green
Write-Host "================================================`n" -ForegroundColor Cyan

Write-Host "📊 Abra em seu navegador:" -ForegroundColor Yellow
Write-Host "   http://localhost:8000/" -ForegroundColor Cyan
Write-Host "`nOu acesse os detalhes:" -ForegroundColor Yellow  
Write-Host "   http://localhost:8000/ativo-detalhes.html?id=1" -ForegroundColor Cyan

Write-Host "`n⚠️  IMPORTANTE:" -ForegroundColor Yellow
Write-Host "   O servidor está rodando. Deixe este terminal aberto." -ForegroundColor White
Write-Host "   Para parar: Pressione Ctrl+C`n" -ForegroundColor White

# Aguardar que o servidor continue rodando
$serverProcess | Wait-Process
