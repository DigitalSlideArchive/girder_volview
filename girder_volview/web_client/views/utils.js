const origin = globalThis.location.origin;
const volViewPath = `${origin}/static/built/plugins/volview/index.html`;
const fileRoot = `${origin}/api/v1/item`;

export function open(model) {
    const itemRoot = `${fileRoot}/${model.id}`;
    const downloadUrl = `${itemRoot}/download`;
    const saveUrl = `${itemRoot}/volview`;
    const volViewUrl = `${volViewPath}?names=[${model.name()}]&urls=[${downloadUrl}]&save=${saveUrl}`;
    window.open(volViewUrl, "_blank").focus();
}
