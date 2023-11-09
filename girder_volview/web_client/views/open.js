import { restRequest } from "@girder/core/rest";

const origin = globalThis.location.origin;
const volViewPath = `${origin}/static/built/plugins/volview/index.html`;
const apiRoot = `${origin}/api/v1`;
const itemApi = `${apiRoot}/item`;

function isSessionFile(fileName) {
    return fileName.endsWith("volview.zip");
}

function makeDownloadParams(model, itemRoute, files, config) {
    if (files.length === 0) return "";

    const hasSessionFiles = files.some(({ name }) => isSessionFile(name));
    const { url:downloadUrl, name } = hasSessionFiles
        ? { url:`${itemRoute}/volview`, name: `${model.name()}.volview.zip` }
        : { url:`${itemRoute}/volview/datasets`, name: `${model.name()}-files.json` }

    const configUrl = `${itemRoute}/volview/config/.volview_config.yaml`;

    return `&names=[${name},config.json]&urls=[${downloadUrl},${configUrl}]`;
}

export function open(model) {
    restRequest({
        type: "GET",
        url: `item/${model.id}/files?limit=0`,
        error: null,
    })
        .done((files) => {
            restRequest({
                url: `folder/${model.get(
                    "folderId"
                )}/yaml_config/.volview_config.yaml`,
            }).done((config) => {
                const itemRoute = `${itemApi}/${model.id}`;
                const saveParam = `&save=${itemRoute}/volview`;
                const downloadParams = makeDownloadParams(
                    model,
                    itemRoute,
                    files,
                    config
                );
                const newTabUrl = `${volViewPath}?${saveParam}${downloadParams}`;
                window.open(newTabUrl, "_blank").focus();
            });
        })
        .fail((resp) => {
            events.trigger("g:alert", {
                icon: "cancel",
                text: "Could not check for config file for VolView",
                type: "danger",
                timeout: 4000,
            });
        });
}
