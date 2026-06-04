use argon2::{
    password_hash::{
        rand_core::OsRng,
        PasswordHash, 
        PasswordHasher, 
        PasswordVerifier, 
        SaltString
    },
    Argon2,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use aes_gcm::{
    aead::{Aead, AeadCore, KeyInit,},
    Aes256Gcm, Nonce,
};
use hmac::{Hmac, Mac};
use sha2::Sha256;
type HmacSha256 = Hmac<Sha256>;

#[pyfunction]
fn hash_password(password: &str) -> PyResult<String> {
    let salt = SaltString::generate(&mut OsRng);
    let argon2 = Argon2::default();

    let hash = argon2
        .hash_password(password.as_bytes(), &salt)
        .map_err(|e| PyValueError::new_err(format!("argon2 hash failed: {}", e)))?;
    
    Ok(hash.to_string())
}

#[pyfunction]
fn verify_password(password: &str, hash: &str) -> PyResult<bool> {
  let parsed = PasswordHash::new(hash)
    .map_err(|e| PyValueError::new_err(format!("invalid hash format: {}", e)))?;

    Ok(Argon2::default()
        .verify_password(password.as_bytes(), &parsed)
        .is_ok())
}


#[pymodule]
fn ticket_secrets(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(hash_password, m)?)?;
    m.add_function(wrap_pyfunction!(verify_password, m)?)?;
    m.add_function(wrap_pyfunction!(aes_gcm_encrypt, m)?)?;
    m.add_function(wrap_pyfunction!(aes_gcm_decrypt, m)?)?;
    m.add_function(wrap_pyfunction!(hmac_sha256, m)?)?;
    Ok(())
}



#[pyfunction]
fn aes_gcm_encrypt(key: &[u8], plaintext: &[u8]) -> PyResult<Vec<u8>>{
    if key.len() != 32 {
        return Err(PyValueError::new_err(format!("Key must be 32 bytes, got {}", key.len())));
    }
    
    let cipher = Aes256Gcm::new_from_slice(key)
        .map_err(|e| PyValueError::new_err(format!("Failed to create cipher: {}", e)))?;
    
    let nonce = Aes256Gcm::generate_nonce(&mut OsRng);

    let ciphertext = cipher.encrypt(&nonce, plaintext)
        .map_err(|e| PyValueError::new_err(format!("Encryption failed: {}", e)))?;
    
    let mut output =Vec::with_capacity(nonce.len() + ciphertext.len());
    output.extend_from_slice(&nonce);
    output.extend_from_slice(&ciphertext);
    Ok(output)
}


#[pyfunction]
fn aes_gcm_decrypt(key: &[u8], data: &[u8]) -> PyResult<Vec<u8>> {
    if key.len() != 32 {
        return Err(PyValueError::new_err(format!("Key must be 32 bytes, got {}", key.len())));
    }
    if data.len() < 12 + 16 {
        return Err(PyValueError::new_err("data too short (need nonce + tag)"));
    }

    let (nonce_bytes, ciphertext) = data.split_at(12);
    let nonce = Nonce::from_slice(nonce_bytes);

    let cipher = Aes256Gcm::new_from_slice(key)
        .map_err(|e| PyValueError::new_err(format!("invalid key: {}", e)))?;

    cipher
        .decrypt(nonce, ciphertext)
        .map_err(|e| PyValueError::new_err(format!("decrypt failed: {}", e)))
}


#[pyfunction]
fn hmac_sha256(key: &[u8], message: &[u8]) -> PyResult<Vec<u8>> {
    let mut mac = <HmacSha256 as Mac>::new_from_slice(key)
        .map_err(|e| PyValueError::new_err(format!("invalid HMAC key: {}", e)))?;

    mac.update(message);
    Ok(mac.finalize().into_bytes().to_vec())
}