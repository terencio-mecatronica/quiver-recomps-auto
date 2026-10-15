# Quiver Recomps Auto — listas atualizadas pelo GitHub Actions

Um projeto Python para manter automaticamente duas listas personalizadas do **Quiver Launcher** a partir de repositórios públicos originais que publicam manifestos `.recomp.json` (ou `.recomp-board.json`) ou que foram curados manualmente com referência à documentação do próprio autor.

**Não coleta, copia ou reproduz o catálogo do recomp.fyi.** Respeita os termos da coleção do site. Implementa apenas o [formato de manifesto aberto](https://recomp.fyi/spec) **(recomp.board, CC BY 4.0)** e consulta APIs oficiais de GitHub/GitLab.

## O que é produzido

| Arquivo | Conteúdo | Pronto para importar no Quiver? |
|---|---|---|
| `output/windows-jogaveis.json` | Recomps/ports com status `playable`, `released` ou `complete`, Windows declarado e release com pacote Windows detectável | Sim; **ainda requer avaliação e possivelmente arquivos originais** |
| `output/em-desenvolvimento.json` | Projetos com status explícito `exploring`, `in-progress` ou `paused` | Sim como **lista de acompanhamento**, mas não promete download/execução |
| `output/tudo-quiver.json` | As duas listas acima **mais sua lista anterior de 114 entradas**, deduplicando por repositório e pasta | Sim; recomendada para o uso cotidiano |
| `output/relatorio.json` | Auditoria das decisões, motivos e evidência de builds encontrados | Não; somente para consulta |

⚠️ **Estado inicial:** os JSONs de recomps começam vazios até a primeira execução com acesso às APIs. O combinado contém as suas 114 entradas anteriores. Isto **não** significa que exista zero projeto público; apenas que a primeira consulta ainda não foi realizada. O ZIP entregue foi testado **offline**, não foi executado contra as APIs ao vivo neste ambiente.

## 1. Publicar no seu GitHub (Windows)

1. Crie um repositório público em [github.com/new](https://github.com/new), por exemplo `quiver-recomps-auto`. Escolha **Public** para os arquivos JSON ficarem acessíveis por URL sem autenticação. Não marque a criação de README se preferir enviar a pasta já preparada.
2. Extraia o ZIP. Entre na pasta `quiver-recomps-auto` com Git Bash ou PowerShell (com Git instalado).
3. Execute os comandos abaixo, trocando `SEU_USUARIO` pelo seu login do GitHub:

```bash
git init
git branch -M main
git add .
git commit -m "Publicar gerador Quiver"
git remote add origin https://github.com/SEU_USUARIO/quiver-recomps-auto.git
git push -u origin main
```

Se o repositório GitHub já tiver arquivos próprios, clone-o e **copie o conteúdo da pasta** do ZIP para dentro dele antes de fazer commit, para não sobrescrever histórico existente. GitHub Desktop também pode ser usado.

**Importante:** o token pessoal que você colou no Quiver não deve entrar no projeto, no Git ou nos arquivos JSON. O workflow recebe automaticamente um `GITHUB_TOKEN` temporário e restrito ao seu repositório.

## 2. Ativar e executar a atualização

1. Abra `https://github.com/SEU_USUARIO/quiver-recomps-auto`.
2. Vá em **Actions → Atualizar listas Quiver → Run workflow** (na primeira vez, talvez seja necessário habilitar Actions).
3. Aguarde a execução. O workflow roda testes, consulta os repositórios, gera os JSONs e faz commit apenas se houver mudança.
4. O agendamento é **diário às 09:23 UTC**; a execução real pode atrasar por fila do GitHub.
5. Se o `git push` falhar por permissão, confira **Settings → Actions → General → Workflow permissions** e as regras de proteção da branch; a organização pode impedir que o bot grave. O YAML solicita apenas `contents: write`.

A ação foi configurada para **cancelar a publicação** se houver erro de API/limite de requisições, preservando os JSONs publicados anteriormente. Não há scraping da interface do recomp.fyi.

### URLs finais

Substitua `SEU_USUARIO` pelo seu login real (e o nome do repositório caso tenha escolhido outro):

- **Todos (recomendado):** `https://raw.githubusercontent.com/SEU_USUARIO/quiver-recomps-auto/main/output/tudo-quiver.json`
- **Recomps/ports Windows:** `https://raw.githubusercontent.com/SEU_USUARIO/quiver-recomps-auto/main/output/windows-jogaveis.json`
- **Em desenvolvimento:** `https://raw.githubusercontent.com/SEU_USUARIO/quiver-recomps-auto/main/output/em-desenvolvimento.json`

No Quiver: **Settings → Advanced → My App List** → cole a URL → **Save list**. O Quiver documenta **somente uma My App List ativa por vez**; por isso o arquivo `tudo-quiver.json` agrupa todas para uso normal.

**O GitHub atualizará os arquivos sem intervenção manual.** Entretanto, o Quiver pode precisar de reabertura ou recarregamento para reler o endereço remoto. A versão 3.5 não promete sincronização instantânea do catálogo pessoal.

## 3. Como descobrir novos projetos

Há duas entradas possíveis:

**A. Descoberta automática (GitHub):** o script consulta a busca oficial de código do GitHub por `.recomp.json` e `.recomp-board.json`, **somente na raiz do repositório**. O resultado é conferido com a API de cada repositório, e o manifesto é obtido diretamente dele. A busca é limitada a duas páginas por nome para controlar custos e limites; não equivale a descobrir toda a internet. Use `autoDiscoverMaxPages` em `config/sources.json` para ampliar (até 5, com o risco de limite/rate). A atualização percorre **até 60 repositórios por dia**, alternando o grupo consultado entre as execuções; pode levar alguns dias para revisar os repositórios descobertos. Altere `maxRepositoriesPerRun` se necessário. O snapshot anterior é preservado para não desaparecerem entradas entre lotes.

**B. Fontes selecionadas:** acrescente repositórios reais em `config/sources.json`. Para repositórios sem manifesto, um mantenedor da lista pode registrar nome, tipo, status revisado e URL de evidência; o arquivo JSON não inventa status de projetos não identificados. `curatedStatus` e `curatedWindows` devem ser usados só quando a documentação oficial comprovar esses dados; se houver manifesto, ele tem prioridade.

Exemplo de projeto com manifesto nativo:

```json
{"host": "github", "repo": "ORGANIZACAO/REPOSITORIO"}
```

Exemplo GitLab.com (se o repositório expuser `.recomp.json` na branch padrão):

```json
{"host": "gitlab", "repo": "GRUPO/PROJETO"}
```

Repositórios **Codeberg**: o site permite manifestos neles, mas a documentação publicada do My App List do Quiver só declara `repositorySource` para GitHub e GitLab. Para não inserir entradas que o Quiver possa tratar como GitHub incorretamente, o gerador **ainda não os coloca nos JSONs**. Isso pode ser estendido depois de validar o suporte real da sua versão.

## 4. Critérios de classificação (conservadores)

- **Jogável no Windows:** `type` inclui `recomp` ou `port`, status declarado `playable/released/complete`, destino Windows declarado pelo mantenedor **e** release não-prerelease com arquivo `.exe/.zip/.7z` etc. cujo nome indica Windows (`win64`, `windows`, etc.). Nos três projetos curados iniciais, status/plataforma são anotações humanas fundamentadas nos repositórios oficiais; a detecção de release continua automática.
- **Em desenvolvimento:** somente `exploring`, `in-progress` ou `paused` declarados pelo autor (ou de fonte curada). Sem release? Pode aparecer nessa lista, mas **não deve ser interpretado como instalável**.
- **Ignorados na importação:** decompilações puras, status ausente, Windows não confirmado, release sem pacote reconhecível e projetos removidos. Aparecem em `relatorio.json` quando consultados.
- **Sem ROMs, ISOs ou arquivos de jogos:** os arquivos incluem metadados/repositórios públicos, não material protegido. Alguns ports exigem uma cópia legítima do jogo original.
- **Sem thumbnails pessoais automáticas:** `appIconUrl` fica `null`, exceto URL HTTPS oficial aprovada manualmente em `config/overrides.json`.

> A detecção por nome do arquivo é **uma heurística**, não teste de execução. Um projeto com versão jogável distribuída fora de GitHub Releases pode ser omitido. Ao contrário, uma release com nome Windows não garante que funcione sem configuração. O Quiver pode exigir confirmação adicional para versões sem verificação de segurança própria.

## 5. Editar as fontes, imagens e exclusões

- `config/sources.json`: repositórios monitorados e busca automática.
- `config/overrides.json`: `appIconUrl` para logo/capa oficial, `releaseAssetFilter` para escolher um asset específico; `excludedRepositories` para bloquear entradas (`github:ORGANIZACAO/REPO`).
- `config/minha-lista-114.json`: **sua lista antiga intacta**, usada só no arquivo combinado. Não é classificada automaticamente como coleção de recompilações.
- `data/discovered_repos.json`: cache dos repositórios de manifesto público encontrados por consulta própria.
- `data/snapshot.json`: estado incremental e classificações já verificadas; necessário para processar repositórios em lotes sem apagar os anteriores.
- `output/relatorio.json`: explica o que foi incluído/ignorado.

**Dica:** não reimporte manualmente `apps.json` da biblioteca local do Quiver; use o campo **My App List**. Não coloque tokens no código/arquivos. Para GitLab público, o token é opcional. Para fornecer um token privado ao workflow, use **Settings → Secrets and variables → Actions → New repository secret** com o nome `GITLAB_TOKEN`.

## 6. Testar localmente

Requer Python 3.11+ e não instala dependências de terceiros.

```bash
python -m unittest discover -s tests -v
python scripts/update.py --no-discovery
python scripts/validate.py
```

O comando `update.py` precisa de conexão com GitHub/GitLab quando executado de verdade. Sem `GITHUB_TOKEN`, usa apenas fontes curadas e o cache; os limites de API não autenticados são menores. Se não quiser executar localmente, deixe o GitHub Actions cuidar das atualizações.

## Limites e referências

- [Especificação `.recomp.json` (CC BY 4.0)](https://recomp.fyi/spec) — formato aberto de autoria do recomp.board.
- [Termos do recomp.fyi](https://recomp.fyi/) — proíbem copiar a coleção sem autorização; **este projeto não faz isso**.
- [README do Quiver Launcher](https://github.com/tgeorgiadis/quiver-launcher#your-own-app-list) — JSON `apps`, URL remota, uma lista ativa.
- [GitHub Actions: `GITHUB_TOKEN` e permissões](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token).
- [GitHub API code search](https://docs.github.com/en/rest/search/search#search-code) — requer autenticação, possui limites e indexação incompleta.

Este é um gerador independente, não afiliado ao recomp.board, Quiver, GitHub ou aos autores dos jogos. Respeite a licença de cada projeto.
