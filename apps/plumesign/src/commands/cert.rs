//! 一键签证书模式：读 .p12 + 描述文件的信息给界面显示，并检查两者是否配套。
//! 输出一行一项，方便 Python 解析：
//!   PLUME_CERT name\t<证书名>
//!   PLUME_CERT team\t<团队 ID>
//!   PLUME_CERT expires\t<unix 秒>
//!   PLUME_CERT sha1\t<指纹>
//!   PLUME_PROV name|team|expires|app_id|all_devices|cert_match\t<值>
//!   PLUME_PROV devices\t<udid,udid,...>
use std::path::PathBuf;

use anyhow::Result;
use clap::Args;
use plist::Value;
use plume_core::CertificateIdentity;

#[derive(Debug, Args)]
#[command(arg_required_else_help = true)]
pub struct CertArgs {
    /// .p12 证书
    #[arg(long = "p12", value_name = "P12")]
    pub p12: PathBuf,
    /// .p12 密码（也可用环境变量 PLUME_P12_PASSWORD）
    #[arg(long = "p12-password", value_name = "PASSWORD")]
    pub p12_password: Option<String>,
    /// 描述文件 .mobileprovision（可选）
    #[arg(long = "provision", value_name = "PROVISION")]
    pub provision: Option<PathBuf>,
}

fn prov_plist(data: &[u8]) -> Option<plist::Dictionary> {
    let start = data.windows(6).position(|w| w == b"<plist")?;
    let end = data.windows(8).rposition(|w| w == b"</plist>")? + 8;
    Value::from_reader_xml(&data[start..end]).ok()?.into_dictionary()
}

fn clean(s: &str) -> String {
    s.replace(['\t', '\n', '\r'], " ")
}

pub async fn execute(args: CertArgs) -> Result<()> {
    let pw = args
        .p12_password
        .or_else(|| std::env::var("PLUME_P12_PASSWORD").ok())
        .unwrap_or_default();
    let data = std::fs::read(&args.p12)?;
    let id = CertificateIdentity::new_with_p12(&data, &pw)?;
    let cert = id.cert.as_ref().ok_or_else(|| anyhow::anyhow!("P12_NO_CERTIFICATE"))?;
    let leaf_der = cert.encode_der()?;

    let name = cert.subject_common_name().unwrap_or_default();
    let team = cert
        .subject_name()
        .iter_organizational_unit()
        .find_map(|ou| ou.to_string().ok())
        .unwrap_or_default();
    println!("PLUME_CERT name\t{}", clean(&name));
    println!("PLUME_CERT team\t{}", clean(&team));
    println!("PLUME_CERT expires\t{}", cert.validity_not_after().timestamp());
    println!("PLUME_CERT sha1\t{}", hex_upper(cert.sha1_fingerprint()?.as_ref()));

    if let Some(path) = args.provision {
        let raw = std::fs::read(&path)?;
        let d = prov_plist(&raw).ok_or_else(|| anyhow::anyhow!("PROV_BAD_FORMAT"))?;
        let s = |k: &str| d.get(k).and_then(Value::as_string).unwrap_or("").to_string();
        let team_ids = d
            .get("TeamIdentifier")
            .and_then(Value::as_array)
            .and_then(|a| a.first())
            .and_then(Value::as_string)
            .unwrap_or("")
            .to_string();
        let exp = d
            .get("ExpirationDate")
            .and_then(Value::as_date)
            .map(|t| std::time::SystemTime::from(t)
                .duration_since(std::time::UNIX_EPOCH)
                .map(|x| x.as_secs())
                .unwrap_or(0))
            .unwrap_or(0);
        let ent = d.get("Entitlements").and_then(Value::as_dictionary);
        let app_id = ent
            .and_then(|e| e.get("application-identifier"))
            .and_then(Value::as_string)
            .unwrap_or("")
            .to_string();
        let all = d.get("ProvisionsAllDevices").and_then(Value::as_boolean).unwrap_or(false);
        let devices: Vec<String> = d
            .get("ProvisionedDevices")
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(Value::as_string).map(str::to_string).collect())
            .unwrap_or_default();
        let cert_match = d
            .get("DeveloperCertificates")
            .and_then(Value::as_array)
            .map(|a| a.iter().any(|c| c.as_data().map(|b| b == leaf_der.as_slice()).unwrap_or(false)))
            .unwrap_or(false);
        println!("PLUME_PROV name\t{}", clean(&s("Name")));
        println!("PLUME_PROV team\t{}", clean(&team_ids));
        println!("PLUME_PROV team_name\t{}", clean(&s("TeamName")));
        println!("PLUME_PROV expires\t{}", exp);
        println!("PLUME_PROV app_id\t{}", clean(&app_id));
        println!("PLUME_PROV all_devices\t{}", all);
        println!("PLUME_PROV cert_match\t{}", cert_match);
        println!("PLUME_PROV devices\t{}", devices.join(","));
    }
    Ok(())
}

fn hex_upper(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02X}")).collect()
}
