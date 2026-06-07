# Red GitHub de Bitcoin, Ethereum y Solana

Este proyecto recoge los contribuyentes publicados por GitHub y las relaciones
publicas de seguimiento entre ellos para estos repositorios nucleo:

- Bitcoin: `bitcoin/bitcoin`
- Ethereum: `ethereum/go-ethereum`
- Solana historico: `solana-labs/solana`
- Solana actual: `anza-xyz/agave`

La red es dirigida: una arista `A -> B` significa que la cuenta `A` sigue a la
cuenta `B`. Solo se guardan aristas en las que ambas cuentas pertenecen al
conjunto de contribuyentes.

## Ejecucion

```powershell
python recolectar_github_cripto.py
```

La ejecucion usa cache en `datos_github_cripto/cache`, por lo que puede
reanudarse con el mismo comando. Si existe `GH_TOKEN` o `GITHUB_TOKEN`, el
script lo usa automaticamente para ampliar la cuota REST, pero no es
obligatorio.

## Resultados

- `contribuyentes_github_cripto.csv`: un registro por contribuyente.
- `contribuciones_repos_github_cripto.csv`: contribuciones por repositorio.
- `seguimientos_entre_contribuyentes_github_cripto.csv`: lista de aristas.
- `estado_recoleccion_github_cripto.csv`: cobertura y errores por perfil.
- `grafo_contribuyentes_github_cripto.gexf`: grafo dirigido para Gephi.
- `grafo_gephi_proyectos_y_seguimientos.gexf`: version visual con nodos de
  proyecto, pertenencias, colores y tamanos preparados para Gephi.
- `resumen_recoleccion.json`: alcance, recuentos y limitaciones.

En la version visual hay dos valores en la columna `relationship` de aristas:

- `follows`: una cuenta sigue a otra cuenta.
- `contributes_to`: una persona contribuye a Bitcoin, Ethereum o Solana.

Bitcoin aparece en naranja, Ethereum en azul, Solana en verde, las personas
multiplataforma en morado y los autores anonimos en gris.

Los bots y autores anonimos se conservan y se etiquetan. Los correos de commits
anonimos no se exportan: solo se usan para crear un identificador hash local.
GitHub puede ocultar o no publicar la pestana `following` de determinadas
cuentas; esos casos quedan registrados como `unavailable`.
